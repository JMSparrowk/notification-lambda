import os
import boto3
import logging

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from boto3.dynamodb.conditions import Attr


logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ==============================
# AWS Clients / Resources
# ==============================

dynamodb = boto3.resource("dynamodb")
ses = boto3.client("ses")


USERS_TABLE_NAME = os.environ["USERS_TABLE"]
SCHEDULE_TABLE_NAME = os.environ["SCHEDULE_TABLE"]
SENDER_EMAIL = os.environ["SENDER_EMAIL"]

users_table = dynamodb.Table(USERS_TABLE_NAME)
schedule_table = dynamodb.Table(SCHEDULE_TABLE_NAME)

JST = ZoneInfo("Asia/Tokyo")
CALENDAR_FOOTER = (
    "最新の予定はカレンダーでご確認ください。\n"
    "https://d88l94lniveng.cloudfront.net/"
)


# ==============================
# Lambda Handler
# ==============================

def lambda_handler(event, context):

    logger.info("event=%s", event)

    mode = event.get("mode")

    if mode == "DAILY":
        return send_daily_notification()

    if mode == "WEEKLY":
        return send_weekly_notification()

    logger.error("Unknown mode: %s", mode)

    return {
        "statusCode": 400,
        "message": f"Unknown mode: {mode}"
    }


# ==============================
# DAILY
# ==============================

def send_daily_notification():

    today = datetime.now(JST).date()

    logger.info("Daily notification started. date=%s", today)

    schedule = get_schedule(today)

    # 일정 자체가 없는 경우
    if not schedule:
        logger.info("No schedule found. date=%s", today)
        return success("No schedule")

    assigned_user_id = schedule.get("assignedUserId")

    # 공휴일 / 담당자 없음
    if not assigned_user_id:
        logger.info(
            "No assignee. date=%s holiday=%s",
            today,
            schedule.get("holiday")
        )

        return success("No assignee")

    user = get_user(assigned_user_id)

    if not user:
        logger.warning(
            "User not found. userId=%s",
            assigned_user_id
        )

        return success("User not found")

    # 메일 알림 OFF
    if not user.get("emailNotification", False):
        logger.info(
            "Email notification disabled. userId=%s",
            assigned_user_id
        )

        return success("Notification disabled")

    email = user.get("email")

    if not email:
        logger.warning(
            "User has no email. userId=%s",
            assigned_user_id
        )

        return success("No email")

    name = user.get("name", "")

    subject = "【朝礼】本日の担当のお知らせ"

    body = f"""おはようございます。

本日 {format_notification_date(today)} の朝礼担当は {name} さんです。

よろしくお願いいたします。

{CALENDAR_FOOTER}
"""

    send_email(
        to_address=email,
        subject=subject,
        body=body
    )

    logger.info(
        "Daily notification sent. userId=%s",
        assigned_user_id
    )

    return success("Daily email sent")


# ==============================
# WEEKLY
# ==============================

def send_weekly_notification():

    today = datetime.now(JST).date()

    logger.info("Weekly notification started. date=%s", today)

    next_monday = get_next_monday(today)

    schedules = []

    # 월 ~ 금
    for i in range(5):

        target_date = next_monday + timedelta(days=i)

        schedule = get_schedule(target_date)

        schedules.append(
            create_weekly_row(target_date, schedule)
        )

    recipients = get_notification_users()

    logger.info(
        "Weekly recipients count=%s",
        len(recipients)
    )

    if not recipients:
        return success("No recipients")

    subject = (
        f"【朝礼】"
        f"{next_monday.strftime('%m/%d')}週の担当予定"
    )

    body = create_weekly_email_body(schedules)

    # 주소 노출 방지를 위해 한 명씩 발송
    for user in recipients:

        email = user.get("email")

        if not email:
            continue

        send_email(
            to_address=email,
            subject=subject,
            body=body
        )

    logger.info(
        "Weekly notification completed. count=%s",
        len(recipients)
    )

    return success("Weekly email sent")


# ==============================
# DynamoDB
# ==============================

def get_schedule(target_date):

    month = target_date.strftime("%Y-%m")
    date_string = target_date.strftime("%Y-%m-%d")

    response = schedule_table.get_item(
        Key={
            "month": month,
            "date": date_string
        }
    )

    return response.get("Item")


def get_user(user_id):

    response = users_table.get_item(
        Key={
            "userId": user_id
        }
    )

    return response.get("Item")


def get_notification_users():

    response = users_table.scan(
        FilterExpression=Attr("emailNotification").eq(True)
    )

    items = response.get("Items", [])

    # Scan pagination 대응
    while "LastEvaluatedKey" in response:

        response = users_table.scan(
            FilterExpression=Attr(
                "emailNotification"
            ).eq(True),
            ExclusiveStartKey=response[
                "LastEvaluatedKey"
            ]
        )

        items.extend(response.get("Items", []))

    return items


# ==============================
# Date
# ==============================

def format_notification_date(target_date):

    weekday = "月火水木金土日"[target_date.weekday()]
    return f"{target_date.strftime('%Y年%m月%d日')} {weekday}曜日"


def get_next_monday(today):

    days_until_monday = (7 - today.weekday()) % 7

    # 일요일 실행이면 자동으로 다음날 월요일
    if days_until_monday == 0:
        days_until_monday = 7

    return today + timedelta(days=days_until_monday)


# ==============================
# Weekly Mail
# ==============================

def create_weekly_row(target_date, schedule):

    weekdays = [
        "月",
        "火",
        "水",
        "木",
        "金",
        "土",
        "日"
    ]

    weekday = weekdays[target_date.weekday()]

    if not schedule:
        return {
            "date": target_date,
            "weekday": weekday,
            "name": "",
            "holidayName": ""
        }

    return {
        "date": target_date,
        "weekday": weekday,
        "name": schedule.get(
            "assignedUserName",
            ""
        ),
        "holidayName": schedule.get(
            "holidayName",
            ""
        )
    }


def create_weekly_email_body(schedules):

    lines = [
        "お疲れ様です。",
        "",
        "来週の朝礼担当をお知らせします。",
        ""
    ]

    for schedule in schedules:

        date = schedule["date"]
        name = schedule["name"]
        holiday_name = schedule["holidayName"]

        line = (
            f"{format_notification_date(date)} "
            f"{name}"
        )

        if holiday_name:
            line += f"　{holiday_name}"

        lines.append(line)

    lines.extend([
        "",
        "よろしくお願いいたします。",
        "",
        CALENDAR_FOOTER
    ])

    return "\n".join(lines)


# ==============================
# SES
# ==============================

def send_email(to_address, subject, body):

    response = ses.send_email(
        Source=SENDER_EMAIL,
        Destination={
            "ToAddresses": [
                to_address
            ]
        },
        Message={
            "Subject": {
                "Data": subject,
                "Charset": "UTF-8"
            },
            "Body": {
                "Text": {
                    "Data": body,
                    "Charset": "UTF-8"
                }
            }
        }
    )

    logger.info(
        "SES message sent. messageId=%s to=%s",
        response.get("MessageId"),
        to_address
    )


def success(message):

    return {
        "statusCode": 200,
        "message": message
    }
