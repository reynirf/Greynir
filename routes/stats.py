"""

    Reynir: Natural language processing for Icelandic

    Copyright (C) 2019 Miðeind ehf.

       This program is free software: you can redistribute it and/or modify
       it under the terms of the GNU General Public License as published by
       the Free Software Foundation, either version 3 of the License, or
       (at your option) any later version.
       This program is distributed in the hope that it will be useful,
       but WITHOUT ANY WARRANTY; without even the implied warranty of
       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
       GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see http://www.gnu.org/licenses/.


    Stats-related routes

"""


from . import routes, max_age

import json
from datetime import datetime, timedelta
from decimal import Decimal

from flask import request, render_template

from settings import changedlocale
from db import SessionContext
from db.queries import StatsQuery, ChartsQuery, GenderQuery, BestAuthorsQuery
from reynir.bindb import BIN_Db


DEFAULT_STATS_PERIOD = 10  # days
MAX_STATS_PERIOD = 30  # days
_TOP_AUTHORS_PERIOD = 30  # days


def chart_stats(session=None, num_days=7):
    """ Return scraping and parsing stats for charts """

    # TODO: This should be put in a column in the roots table
    colors = {
        "Kjarninn": "#f17030",
        "RÚV": "#dcdcdc",
        "Vísir": "#3d6ab9",
        "Morgunblaðið": "#020b75",
        "Eyjan": "#ca151c",
        "Kvennablaðið": "#900000",
        "Stundin": "#ee4420",
        "Hringbraut": "#44607a",
        "Fréttablaðið": "#002a61",
        "Hagstofa Íslands": "#818285",
    }

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    labels = []
    sources = {}
    parsed_data = []

    # Get article count for each source for each day
    # We change locale to get localized date weekday/month names
    with changedlocale(category="LC_TIME"):
        for n in range(0, num_days):
            days_back = num_days - n - 1
            start = today - timedelta(days=days_back)
            end = today - timedelta(days=days_back - 1)

            # Generate label
            if start < today - timedelta(days=6):
                labels.append(start.strftime("%-d. %b"))
            else:
                labels.append(start.strftime("%A"))

            sent = 0
            parsed = 0

            # Get article count per source for day
            # Also collect parsing stats
            q = ChartsQuery.period(start, end, enclosing_session=session)
            for (name, cnt, s, p) in q:
                sources.setdefault(name, []).append(cnt)
                sent += s
                parsed += p

            percent = round((parsed / sent) * 100, 2) if sent else 0
            parsed_data.append(percent)

    # Create datasets for bar chart
    datasets = []
    article_count = 0
    for k, v in sorted(sources.items()):
        color = colors.get(k, "#000")
        datasets.append({"label": k, "backgroundColor": color, "data": v})
        article_count += sum(v)

    # Calculate averages
    scrape_avg = article_count / num_days
    parse_avg = sum(parsed_data) / num_days

    return {
        "scraped": {"labels": labels, "datasets": datasets, "avg": scrape_avg},
        "parsed": {
            "labels": labels,
            "datasets": [{"data": parsed_data}],
            "avg": parse_avg,
        },
    }


def top_authors(days=_TOP_AUTHORS_PERIOD, session=None):
    end = datetime.utcnow()
    start = end - timedelta(days=_TOP_AUTHORS_PERIOD)
    authors = BestAuthorsQuery.period(
        start, end, enclosing_session=session, min_articles=10
    )[:20]

    authresult = list()
    with BIN_Db.get_db() as bindb:
        for a in authors:
            name = a[0]
            gender = bindb.lookup_name_gender(name)
            if gender == "hk":  # Skip unnamed authors (e.g. "Ritstjórn Vísis")
                continue
            perc = round(float(a[4]), 2)
            authresult.append({"name": name, "gender": gender, "perc": perc})

    return authresult[:10]


@routes.route("/stats", methods=["GET"])
@max_age(seconds=30 * 60)
def stats():
    """ Render a page with various statistics """
    days = DEFAULT_STATS_PERIOD
    try:
        days = min(MAX_STATS_PERIOD, int(request.args.get("days")))
    except:
        pass

    with SessionContext(read_only=True) as session:

        # Article stats
        sq = StatsQuery()
        result = sq.execute(session)
        total = dict(art=Decimal(), sent=Decimal(), parsed=Decimal())
        for r in result:
            total["art"] += r.art
            total["sent"] += r.sent
            total["parsed"] += r.parsed

        # Gender stats
        gq = GenderQuery()
        gresult = gq.execute(session)

        gtotal = dict(kvk=Decimal(), kk=Decimal(), hk=Decimal(), total=Decimal())
        for r in gresult:
            gtotal["kvk"] += r.kvk
            gtotal["kk"] += r.kk
            gtotal["hk"] += r.hk
            gtotal["total"] += r.kvk + r.kk + r.hk

        # Author stats
        authresult = top_authors(session=session)

        # Chart stats
        chart_data = chart_stats(session=session, num_days=days)

        return render_template(
            "stats.html",
            result=result,
            total=total,
            gresult=gresult,
            gtotal=gtotal,
            authresult=authresult,
            scraped_chart_data=json.dumps(chart_data["scraped"]),
            parsed_chart_data=json.dumps(chart_data["parsed"]),
            scraped_avg=int(round(chart_data["scraped"]["avg"])),
            parsed_avg=round(chart_data["parsed"]["avg"], 1),
        )