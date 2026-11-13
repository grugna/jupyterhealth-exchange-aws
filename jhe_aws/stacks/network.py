"""The VPC: three tiers across two or three AZs.

    public    the ALB and the NAT; nothing of ours has a public address
    private   every task, with egress through the NAT (EHRs are on the internet)
    isolated  the database; no route out in either direction

S3 is reached through a gateway endpoint (free, and bundle traffic stays off the NAT). Flow logs
go to CloudWatch. In import mode an existing VPC is named by ids, so synth needs no AWS access.
"""

from __future__ import annotations

import hashlib

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from cdk_nag import NagSuppressions
from constructs import Construct

from jhe_aws.config import EnvironmentConfig, NatMode, NetworkMode


def nat_user_data() -> ec2.UserData:
    """The NAT instance's boot script, in place of the provider's: the interface found rather
    than assumed (AL2023 names it ens5), the package install retried (it is what fails on a
    small instance), and the rules and the forwarding setting persisted across reboots. Any
    failure exits non-zero, which the creation policy turns into a failed stack."""
    user_data = ec2.UserData.for_linux()
    user_data.add_commands(
        "set -euo pipefail",
        "for attempt in 1 2 3; do dnf install -y iptables-services && break; sleep 15; done",
        "command -v iptables",
        "IFACE=$(ip -o -4 route show to default | awk '{print $5}')",
        "sysctl -q -w net.ipv4.ip_forward=1",
        'sysctl -q -w "net.ipv4.conf.${IFACE}.send_redirects=0"',
        "printf 'net.ipv4.ip_forward = 1\\nnet.ipv4.conf.%s.send_redirects = 0\\n' \"$IFACE\" "
        "> /etc/sysctl.d/90-nat.conf",
        'iptables -t nat -A POSTROUTING -o "$IFACE" -j MASQUERADE',
        "iptables -F FORWARD",
        "iptables-save > /etc/sysconfig/iptables",
        "systemctl enable --now iptables",
    )
    return user_data


class NetworkStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, cfg: EnvironmentConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg
        self.vpc = self._import() if cfg.network.mode is NetworkMode.IMPORT else self._create()
        self.private_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        self.isolated_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)
        self.public_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id)

    def _create(self) -> ec2.IVpc:
        net = self.cfg.network
        if net.nat is NatMode.INSTANCE:
            nat_provider = ec2.NatProvider.instance_v2(
                instance_type=ec2.InstanceType(net.nat_instance_type),
                machine_image=ec2.MachineImage.latest_amazon_linux2023(
                    cpu_type=ec2.AmazonLinuxCpuType.ARM_64
                ),
                default_allowed_traffic=ec2.NatTrafficDirection.OUTBOUND_ONLY,
                user_data=(user_data := nat_user_data()),
            )
            # A boot script runs once, at first boot, and CloudFormation applies a new script or
            # type to an existing instance by stop and start, which would not run it: the
            # fingerprint in the logical id makes any change to either replace the instance.
            self._nat_fingerprint = hashlib.sha256(
                f"{net.nat_instance_type}\n{user_data.render()}".encode()
            ).hexdigest()[:8]
            nat_gateways = 1  # one instance; the sandbox trades an AZ's redundancy for cost
        else:
            nat_provider = ec2.NatProvider.gateway()
            nat_gateways = net.num_azs
        vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(net.cidr),
            max_azs=net.num_azs,
            nat_gateway_provider=nat_provider,
            nat_gateways=nat_gateways,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(
                    name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
            restrict_default_security_group=True,
        )
        vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)
        if net.nat is NatMode.INSTANCE:
            # OUTBOUND_ONLY above means the provider adds no inbound rule (its default admits the
            # whole internet); the private subnets' traffic has to be let in, and only theirs.
            nat_provider.security_group.add_ingress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.all_traffic(), "from inside the VPC, to be NATed"
            )
            NagSuppressions.add_resource_suppressions(
                nat_provider.security_group,
                [
                    {
                        "id": "AwsSolutions-EC23",
                        "reason": "The ingress CIDR is the VPC's own block, a token at synth that the "
                        "rule cannot read; it is not 0.0.0.0/0.",
                    }
                ],
            )
            self._harden_nat_instances(vpc)
        if net.flow_logs:
            log_group = logs.LogGroup(
                self,
                "FlowLogs",
                log_group_name=f"/jhe/{self.cfg.name}/vpc-flow-logs",
                retention=_retention(self.cfg.log_retention_days),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            ec2.FlowLog(
                self,
                "FlowLog",
                resource_type=ec2.FlowLogResourceType.from_vpc(vpc),
                destination=ec2.FlowLogDestination.to_cloud_watch_logs(log_group),
                traffic_type=ec2.FlowLogTrafficType.ALL,
            )
        return vpc

    def _harden_nat_instances(self, vpc: ec2.Vpc) -> None:
        """The NAT instance the provider creates: its root volume encrypted (the provider has no
        option for it, so set on the instance it creates); the stack waits for the boot script
        to signal and fails with its exit code if it does not, so a NAT that is not forwarding
        never deploys as a success; and the two findings that do not apply to a stateless,
        replaceable NAT suppressed with their reasons."""
        instances = [c for c in vpc.node.find_all() if isinstance(c, ec2.Instance)]
        for instance in instances:
            cfn = instance.node.default_child
            assert isinstance(cfn, ec2.CfnInstance)
            cfn.override_logical_id(f"{self.get_logical_id(cfn)}{self._nat_fingerprint}")
            if len(instances) == 1:  # the provider shares one UserData between its instances
                cfn.cfn_options.creation_policy = cdk.CfnCreationPolicy(
                    resource_signal=cdk.CfnResourceSignal(count=1, timeout="PT10M")
                )
                instance.user_data.add_signal_on_exit_command(instance)
            cfn.block_device_mappings = [
                ec2.CfnInstance.BlockDeviceMappingProperty(
                    device_name="/dev/xvda",
                    ebs=ec2.CfnInstance.EbsProperty(encrypted=True, volume_type="gp3"),
                )
            ]
            NagSuppressions.add_resource_suppressions(
                instance,
                [
                    {
                        "id": "AwsSolutions-EC28",
                        "reason": "Detailed (one-minute) monitoring on a NAT instance buys nothing the "
                        "VPC flow logs and the five-minute metrics do not.",
                    },
                    {
                        "id": "AwsSolutions-EC29",
                        "reason": "The NAT instance holds no state and is recreated by the stack; "
                        "termination protection would only block the stack's own replacement of it.",
                    },
                ],
            )

    def _import(self) -> ec2.IVpc:
        existing = self.cfg.network.existing
        assert existing is not None
        return ec2.Vpc.from_vpc_attributes(
            self,
            "Vpc",
            vpc_id=existing.vpc_id,
            availability_zones=existing.availability_zones,
            public_subnet_ids=existing.public_subnet_ids,
            private_subnet_ids=existing.private_subnet_ids,
            isolated_subnet_ids=existing.isolated_subnet_ids,
        )


_RETENTION = (
    (1, "ONE_DAY"),
    (3, "THREE_DAYS"),
    (5, "FIVE_DAYS"),
    (7, "ONE_WEEK"),
    (14, "TWO_WEEKS"),
    (30, "ONE_MONTH"),
    (60, "TWO_MONTHS"),
    (90, "THREE_MONTHS"),
    (120, "FOUR_MONTHS"),
    (150, "FIVE_MONTHS"),
    (180, "SIX_MONTHS"),
    (365, "ONE_YEAR"),
    (400, "THIRTEEN_MONTHS"),
    (545, "EIGHTEEN_MONTHS"),
    (731, "TWO_YEARS"),
    (1096, "THREE_YEARS"),
    (1827, "FIVE_YEARS"),
    (2192, "SIX_YEARS"),
    (2557, "SEVEN_YEARS"),
    (2922, "EIGHT_YEARS"),
    (3288, "NINE_YEARS"),
    (3653, "TEN_YEARS"),
)


def _retention(days: int) -> logs.RetentionDays:
    """The RetentionDays member for a day count CloudWatch accepts: the count itself, or the
    next longer one; past ten years, no expiry."""
    for value, member in _RETENTION:
        if value >= days:
            return getattr(logs.RetentionDays, member)
    return logs.RetentionDays.INFINITE
