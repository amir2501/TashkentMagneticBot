import os
import re
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "❌ TELEGRAM_BOT_TOKEN не найден в файле .env"
    )


# Tashkent = UTC+5
TASHKENT_TZ = timezone(timedelta(hours=5))


# Public NWS page containing NOAA SWPC DAYTDF product
FORECAST_URL = (
    "https://forecast.weather.gov/product.php"
    "?format=TXT"
    "&glossary=1"
    "&issuedby=TDF"
    "&product=DAY"
    "&site=LCH"
    "&version=1"
)


# ============================================================
# HTTP
# ============================================================

def get_forecast_page():
    """
    Download the latest NOAA/NWS 3-day geomagnetic forecast.
    """

    headers = {
        "User-Agent": (
            "TashkentGeomagneticBot/1.0 "
            "(Telegram bot; educational project)"
        )
    }

    response = requests.get(
        FORECAST_URL,
        headers=headers,
        timeout=20,
    )

    response.raise_for_status()

    if not response.text.strip():
        raise RuntimeError(
            "Источник прогноза вернул пустой ответ."
        )

    return response.text


# ============================================================
# HTML → TEXT
# ============================================================

def clean_html(text):
    """
    NWS returns the product inside an HTML <pre>.
    Remove HTML tags and decode basic entities.
    """

    # Remove scripts
    text = re.sub(
        r"<script.*?</script>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Replace common HTML entities
    replacements = {
        "&nbsp;": " ",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&#39;": "'",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Remove HTML tags
    text = re.sub(
        r"<[^>]+>",
        "",
        text,
    )

    return text


# ============================================================
# Kp → G LEVEL
# ============================================================

def kp_to_g(kp):
    """
    NOAA geomagnetic storm scale.

    Kp < 5  -> no storm
    Kp 5    -> G1
    Kp 6    -> G2
    Kp 7    -> G3
    Kp 8    -> G4
    Kp 9    -> G5
    """

    if kp < 5:
        return None

    if kp < 6:
        return "G1"

    if kp < 7:
        return "G2"

    if kp < 8:
        return "G3"

    if kp < 9:
        return "G4"

    return "G5"


def g_emoji(level):
    if level == "G1":
        return "🟠"

    if level == "G2":
        return "🔴"

    if level == "G3":
        return "🔴"

    if level == "G4":
        return "🟣"

    if level == "G5":
        return "🟣"

    return "🟢"


# ============================================================
# PARSE DATE
# ============================================================

def parse_forecast_dates(text):
    """
    Find the line:

        NOAA Kp index breakdown Aug 26-Aug 28 2026

    """

    pattern = (
        r"NOAA\s+Kp\s+index\s+breakdown\s+"
        r"([A-Z][a-z]{2}\s+\d{1,2})-"
        r"([A-Z][a-z]{2}\s+\d{1,2})\s+"
        r"(\d{4})"
    )

    match = re.search(
        pattern,
        text,
        re.IGNORECASE,
    )

    if not match:
        raise RuntimeError(
            "Не удалось определить даты прогноза NOAA."
        )

    start_text = match.group(1)
    end_text = match.group(2)
    year = int(match.group(3))

    start_date = datetime.strptime(
        f"{start_text} {year}",
        "%b %d %Y",
    ).date()

    end_date = datetime.strptime(
        f"{end_text} {year}",
        "%b %d %Y",
    ).date()

    dates = []

    current = start_date

    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)

    return dates


# ============================================================
# PARSE KP TABLE
# ============================================================

def parse_kp_table(text):
    """
    Extract the NOAA Kp table.

    Example:

                 Aug 26       Aug 27       Aug 28
    00-03UT       0.33         3.67         5.67 (G2)
    03-06UT       0.67         3.33         4.67 (G1)
    ...
    """

    dates = parse_forecast_dates(text)

    # Find table
    table_match = re.search(
        r"NOAA\s+Kp\s+index\s+breakdown.*?"
        r"\n\s*"
        r"([A-Z][a-z]{2}\s+\d{1,2}"
        r"(?:\s+"
        r"[A-Z][a-z]{2}\s+\d{1,2}){2}"
        r")\s*\n"
        r"(.*?)(?:\n\s*Rationale:)",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    if not table_match:
        raise RuntimeError(
            "Не удалось найти таблицу Kp в прогнозе NOAA."
        )

    table_body = table_match.group(2)

    forecast = {
        date: []
        for date in dates
    }

    interval_pattern = re.compile(
        r"^\s*"
        r"(\d{2})-(\d{2})UT"
        r"\s+"
        r"([0-9]+(?:\.[0-9]+))"
        r"\s+"
        r"([0-9]+(?:\.[0-9]+))"
        r"\s+"
        r"([0-9]+(?:\.[0-9]+))",
        re.MULTILINE,
    )

    rows = interval_pattern.findall(
        table_body
    )

    if not rows:
        raise RuntimeError(
            "Таблица Kp найдена, но значения не распознаны."
        )

    for row in rows:

        start_hour = int(row[0])

        kp_values = [
            float(row[2]),
            float(row[3]),
            float(row[4]),
        ]

        for index, date in enumerate(dates):

            kp = kp_values[index]

            forecast[date].append(
                {
                    "start_hour_utc": start_hour,
                    "kp": kp,
                    "g": kp_to_g(kp),
                }
            )

    return forecast


# ============================================================
# UTC → TASHKENT
# ============================================================

def utc_to_tashkent(
    date,
    start_hour_utc,
):
    """
    Convert a UTC interval to Tashkent time.
    """

    dt_utc = datetime(
        date.year,
        date.month,
        date.day,
        start_hour_utc,
        0,
        tzinfo=timezone.utc,
    )

    dt_local = dt_utc.astimezone(
        TASHKENT_TZ
    )

    return dt_local


# ============================================================
# GROUP STORM PERIODS
# ============================================================

def group_storm_periods(
    date,
    periods,
):
    """
    Merge adjacent 3-hour storm periods.
    """

    storm_periods = [
        period
        for period in periods
        if period["g"] is not None
    ]

    if not storm_periods:
        return []

    groups = []

    current = [storm_periods[0]]

    for period in storm_periods[1:]:

        previous = current[-1]

        expected_hour = (
            previous["start_hour_utc"] + 3
        ) % 24

        if (
            period["start_hour_utc"]
            == expected_hour
        ):
            current.append(period)

        else:
            groups.append(current)
            current = [period]

    groups.append(current)

    result = []

    for group in groups:

        start_utc = group[0]["start_hour_utc"]

        last_start_utc = group[-1][
            "start_hour_utc"
        ]

        end_utc = (
            last_start_utc + 3
        ) % 24

        start_local = utc_to_tashkent(
            date,
            start_utc,
        )

        # For 21-00 UTC the end belongs
        # to the following day.
        end_date = date

        if last_start_utc == 21:
            end_date = date + timedelta(days=1)

        end_local = utc_to_tashkent(
            end_date,
            end_utc,
        )

        strongest_kp = max(
            item["kp"]
            for item in group
        )

        strongest_g = kp_to_g(
            strongest_kp
        )

        result.append(
            {
                "start": start_local,
                "end": end_local,
                "kp": strongest_kp,
                "g": strongest_g,
            }
        )

    return result


# ============================================================
# DAY NAME
# ============================================================

def day_title(
    date,
    today,
):
    if date == today:
        return "☀️ Сегодня"

    if date == today + timedelta(days=1):
        return "🌤 Завтра"

    if date == today + timedelta(days=2):
        return "📅 Послезавтра"

    return date.strftime(
        "%d.%m.%Y"
    )


# ============================================================
# FORMAT ONE DAY
# ============================================================

def format_day(
    date,
    today,
    periods,
):
    title = day_title(
        date,
        today,
    )

    storms = group_storm_periods(
        date,
        periods,
    )

    lines = [
        f"**{title}**",
        "",
    ]

    if not storms:

        lines.append(
            "🟢 **Спокойно.**"
        )

        lines.append(
            "Сильных магнитных бурь "
            "не ожидается."
        )

        return "\n".join(lines)

    # We have one or more storm periods
    for storm in storms:

        emoji = g_emoji(
            storm["g"]
        )

        start = storm["start"].strftime(
            "%H:%M"
        )

        end = storm["end"].strftime(
            "%H:%M"
        )

        lines.append(
            f"{emoji} **С {start} до {end} "
            f"ожидается магнитная буря "
            f"{storm['g']}.**"
        )

        # Add Kp only when useful
        lines.append(
            f"Максимальный Kp: "
            f"{storm['kp']:.2f}"
        )

        lines.append("")

    lines.append(
        "В остальное время — спокойно."
    )

    return "\n".join(lines)


# ============================================================
# COMPLETE FORECAST
# ============================================================

def build_forecast_message():

    html = get_forecast_page()

    text = clean_html(
        html
    )

    forecast = parse_kp_table(
        text
    )

    now = datetime.now(
        TASHKENT_TZ
    )

    today = now.date()

    lines = [
        "🌍 **Геомагнитная обстановка**",
        "",
        "📍 Ташкент",
        "",
    ]

    for offset in range(3):

        date = (
            today
            + timedelta(days=offset)
        )

        periods = forecast.get(
            date,
            []
        )

        lines.append(
            format_day(
                date,
                today,
                periods,
            )
        )

        if offset < 2:
            lines.extend(
                [
                    "",
                    "━━━━━━━━━━━━━━",
                    "",
                ]
            )

    lines.extend(
        [
            "",
            "ℹ️ Источник: NOAA / NWS",
        ]
    )

    return "\n".join(lines)


# ============================================================
# TELEGRAM KEYBOARD
# ============================================================

def main_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "🌍 Узнать состояние",
                callback_data="status",
            )
        ]
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# ============================================================
# /start
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "👋 **Здравствуйте!**\n\n"
        "Я показываю прогноз "
        "геомагнитной активности "
        "для Ташкента на ближайшие "
        "3 дня.\n\n"
        "Нажмите кнопку ниже, чтобы "
        "узнать состояние."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


# ============================================================
# STATUS BUTTON
# ============================================================

async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        "⏳ Получаю свежий прогноз..."
    )

    try:

        message = build_forecast_message()

        await query.edit_message_text(
            message,
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )

    except requests.RequestException as e:

        print(
            "NETWORK ERROR:",
            repr(e),
        )

        await query.edit_message_text(
            "❌ Не удалось получить "
            "свежий прогноз.\n\n"
            "Источник прогноза временно "
            "недоступен. Попробуйте "
            "через несколько минут.",
            reply_markup=main_keyboard(),
        )

    except Exception as e:

        print(
            "ERROR:",
            repr(e),
        )

        await query.edit_message_text(
            "❌ Не удалось обработать "
            "свежий прогноз.\n\n"
            "Попробуйте нажать кнопку "
            "ещё раз.",
            reply_markup=main_keyboard(),
        )


# ============================================================
# MAIN
# ============================================================

def main():

    application = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            status,
            pattern="^status$",
        )
    )

    print("Bot is running...")

    application.run_polling()


if __name__ == "__main__":
    main()