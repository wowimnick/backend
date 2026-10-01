resource "aws_sns_topic" "typesense_alerts" {
  name = "${var.name_prefix}-prod-typesense-alerts"
}

resource "aws_sns_topic_subscription" "typesense_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.typesense_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_log_metric_filter" "typesense_raft" {
  name           = "typesense-raft-error"
  log_group_name = aws_cloudwatch_log_group.typesense.name
  pattern        = "?ERROR ?\"Node with no leader\" ?\"can't reset_peer\""

  metric_transformation {
    name          = "typesense-raft-error"
    namespace     = "Classeasily/Typesense"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "typesense_raft" {
  alarm_name          = "${var.name_prefix}-prod-typesense-raft-errors"
  alarm_description   = "Typesense log shows raft/ERROR patterns (sustained)"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  metric_name         = "typesense-raft-error"
  namespace           = "Classeasily/Typesense"
  period              = 300
  statistic           = "Sum"
  threshold           = 3
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.typesense_alerts.arn]
  ok_actions          = [aws_sns_topic.typesense_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "typesense_no_tasks" {
  alarm_name          = "${var.name_prefix}-prod-typesense-no-running-tasks"
  alarm_description   = "No CPU metrics from Typesense ECS service"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "SampleCount"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.typesense_alerts.arn]
  ok_actions          = [aws_sns_topic.typesense_alerts.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.prod.name
    ServiceName = aws_ecs_service.typesense.name
  }
}

resource "aws_cloudwatch_metric_alarm" "typesense_cpu" {
  alarm_name          = "${var.name_prefix}-prod-typesense-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = 85
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.typesense_alerts.arn]
  ok_actions          = [aws_sns_topic.typesense_alerts.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.prod.name
    ServiceName = aws_ecs_service.typesense.name
  }
}

resource "aws_cloudwatch_metric_alarm" "typesense_memory" {
  alarm_name          = "${var.name_prefix}-prod-typesense-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = 85
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.typesense_alerts.arn]
  ok_actions          = [aws_sns_topic.typesense_alerts.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.prod.name
    ServiceName = aws_ecs_service.typesense.name
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.name_prefix}-shared-alb-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 20
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.typesense_alerts.arn]

  dimensions = {
    LoadBalancer = aws_lb.shared.arn_suffix
  }
}
