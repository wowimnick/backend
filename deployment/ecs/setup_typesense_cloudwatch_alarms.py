"""
One-time (idempotent) CloudWatch alarms + SNS for prod Typesense ECS service.

Usage:
  python deployment/ecs/setup_typesense_cloudwatch_alarms.py --email admin@classeasily.com

Requires AWS credentials with cloudwatch, logs, sns, ecs permissions (us-east-2).
"""
from __future__ import annotations

import argparse
import sys

import boto3

REGION = "us-east-2"
ACCOUNT_ID = "459929160817"
CLUSTER = "classeasily-prod-cluster"
SERVICE = "classeasily-prod-typesense-service"
LOG_GROUP = "/ecs/classeasily-prod-typesense"
SNS_TOPIC_NAME = "classeasily-prod-typesense-alerts"
ALARM_PREFIX = "classeasily-prod-typesense"


def _ensure_sns_topic(sns, email: str | None) -> str:
    topics = sns.list_topics().get("Topics", [])
    arn = next(
        (t["TopicArn"] for t in topics if t["TopicArn"].endswith(f":{SNS_TOPIC_NAME}")),
        None,
    )
    if not arn:
        arn = sns.create_topic(Name=SNS_TOPIC_NAME)["TopicArn"]
        print("Created SNS topic:", arn)
    else:
        print("Using SNS topic:", arn)
    if email:
        subs = sns.list_subscriptions_by_topic(TopicArn=arn).get("Subscriptions", [])
        if not any(s.get("Endpoint") == email for s in subs):
            sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=email)
            print(f"Subscribed {email} (confirm via email)")
    return arn


def _put_alarm(cw, name: str, params: dict, sns_arn: str) -> None:
    params = {
        **params,
        "AlarmName": name,
        "AlarmActions": [sns_arn],
        "OKActions": [sns_arn],
        "TreatMissingData": params.get("TreatMissingData", "notBreaching"),
    }
    cw.put_metric_alarm(**params)
    print("Alarm:", name)


def _ensure_log_metric_filter(logs, filter_name: str, pattern: str) -> str:
    try:
        logs.put_metric_filter(
            logGroupName=LOG_GROUP,
            filterName=filter_name,
            filterPattern=pattern,
            metricTransformations=[
                {
                    "metricName": filter_name,
                    "metricNamespace": "Classeasily/Typesense",
                    "metricValue": "1",
                    "defaultValue": 0,
                }
            ],
        )
        print("Metric filter:", filter_name)
    except logs.exceptions.ResourceNotFoundException:
        print(f"[WARN] Log group {LOG_GROUP} not found; create it via ECS deploy first.")
        return ""
    return filter_name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--email",
        help="Admin email for SNS subscription (optional if topic already has subs)",
    )
    args = parser.parse_args()

    sns = boto3.client("sns", region_name=REGION)
    cw = boto3.client("cloudwatch", region_name=REGION)
    logs = boto3.client("logs", region_name=REGION)

    sns_arn = _ensure_sns_topic(sns, args.email)

    # Log-based: raft stuck / generic ERROR lines
    raft_filter = "typesense-raft-error"
    _ensure_log_metric_filter(
        logs,
        raft_filter,
        '?ERROR ?"Node with no leader" ?"can\'t reset_peer"',
    )
    _put_alarm(
        cw,
        f"{ALARM_PREFIX}-raft-errors",
        {
            "AlarmDescription": "Typesense log shows raft/ERROR patterns (sustained)",
            "MetricName": raft_filter,
            "Namespace": "Classeasily/Typesense",
            "Statistic": "Sum",
            "Period": 300,
            "EvaluationPeriods": 2,
            "Threshold": 3,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "TreatMissingData": "notBreaching",
        },
        sns_arn,
    )

    # No ECS CPU samples => service likely has no healthy tasks (TreatMissingData=breaching)
    _put_alarm(
        cw,
        f"{ALARM_PREFIX}-no-running-tasks",
        {
            "AlarmDescription": "No CPU metrics from Typesense ECS service (likely no running tasks)",
            "Namespace": "AWS/ECS",
            "MetricName": "CPUUtilization",
            "Dimensions": [
                {"Name": "ClusterName", "Value": CLUSTER},
                {"Name": "ServiceName", "Value": SERVICE},
            ],
            "Statistic": "SampleCount",
            "Period": 60,
            "EvaluationPeriods": 5,
            "DatapointsToAlarm": 5,
            "Threshold": 1,
            "ComparisonOperator": "LessThanThreshold",
            "TreatMissingData": "breaching",
        },
        sns_arn,
    )

    for metric, suffix, threshold in [
        ("CPUUtilization", "cpu-high", 85),
        ("MemoryUtilization", "memory-high", 85),
    ]:
        _put_alarm(
            cw,
            f"{ALARM_PREFIX}-{suffix}",
            {
                "AlarmDescription": f"Typesense ECS {metric} elevated",
                "Namespace": "AWS/ECS",
                "MetricName": metric,
                "Dimensions": [
                    {"Name": "ClusterName", "Value": CLUSTER},
                    {"Name": "ServiceName", "Value": SERVICE},
                ],
                "Statistic": "Average",
                "Period": 300,
                "EvaluationPeriods": 3,
                "Threshold": threshold,
                "ComparisonOperator": "GreaterThanThreshold",
                "TreatMissingData": "notBreaching",
            },
            sns_arn,
        )

    print("\nDone. Confirm SNS email subscription if newly added.")
    print("Register task def healthCheck: deployment/ecs/classeasily-prod-typesense-task-definition.json")
    sys.exit(0)


if __name__ == "__main__":
    main()
