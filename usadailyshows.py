from aiohttp.helpers import QCONTENT
import requests
import json
import os
import ssl
import random
import asyncio
import aiohttp
from aiohttp_retry import RetryClient, ExponentialRetry
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm
from datetime import datetime, date
from zoneinfo import ZoneInfo

# ================= CONFIG =================

REWRITE_SHOWS = False

FINAL_SUMMARY = []

MAX_WORKERS = 30
CONCURRENCY = 200
ZIP_FILE = "zipcodes.txt"

AUTHORIZATION_TOKEN = "<your-auth-token>"
SESSION_ID = "<your-session-id>"


KNOWN_LANGUAGES = [
"English","Hindi","Tamil","Telugu","Kannada",
"Malayalam","Punjabi","Gujarati","Marathi","Bengali"
]

FORMAT_KEYWORDS = [
"RPX","D-Box","IMAX","EMX","Sony Digital Cinema",
"4DX","ScreenX","Cinemark XD","Dolby Cinema"
]

FORMAT_CODES = {
    "Standard": "S",
    "IMAX": "I",
    "Dolby Cinema": "D",
    "4DX": "4",
    "ScreenX": "X",
    "RPX": "R"
}

LANG_CODES = {
    "English": "E",
    "Hindi": "H",
    "Tamil": "TA",
    "Telugu": "TE",
    "Kannada": "KN",
    "Malayalam": "ML"
}


USER_AGENTS = [
"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/{version} Safari/537.36",
"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}) Gecko/20100101 Firefox/{version}",
"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_{minor}_0) AppleWebKit/537.36 Chrome/{version} Safari/537.36",
]


# ================= RANDOM HELPERS =================

def get_random_user_agent():
    template=random.choice(USER_AGENTS)
    return template.format(
        version=f"{random.randint(70,120)}.0.{random.randint(1000,5000)}.{random.randint(0,150)}",
        minor=random.randint(12,15)
    )

def get_random_ip():
    return ".".join(str(random.randint(1,255)) for _ in range(4))


# ================= HEADERS =================

def get_headers2(zip_code,date):

    ip=get_random_ip()

    return {
        "User-Agent":get_random_user_agent(),
        "Accept":"application/json",
        "Referer":f"https://www.fandango.com/{zip_code}_movietimes?date={date}",
        "X-Forwarded-For":ip,
        "Client-IP":ip
    }


def get_seatmap_headers():

    return {
        "User-Agent":get_random_user_agent(),
        "Origin":"https://fandango.com",
        "Referer":"https://tickets.fandango.com/mobileexpress/seatselection",
        "Authorization":AUTHORIZATION_TOKEN,
        "X-Fd-Sessionid":SESSION_ID,
        "accept":"application/json"
    }
# ================= PARSERS =================

def extract_language(amenities):

    lang_priority = []

    for item in amenities:

        lowered = item.lower()

        for lang in KNOWN_LANGUAGES:

            if f"{lang.lower()} language" in lowered:
                return lang

            if lang.lower() in lowered:

                pos = lowered.find(
                    lang.lower()
                )

                if pos >= 0:

                    lang_priority.append(
                        (
                            lang,
                            pos
                        )
                    )

    if lang_priority:

        lang_priority.sort(
            key=lambda x: x[1]
        )

        return lang_priority[0][0]

    return "English"


def extract_format(amenities,default_format):

    for keyword in FORMAT_KEYWORDS:
        if any(keyword.lower() in a.lower() for a in amenities):
            return keyword

    return default_format


def prepare_showtimes(movie):

    out=[]

    for variant in movie.get("variants",[]):

        fmt=variant.get("formatName","Standard")

        for ag in variant.get("amenityGroups",[]):

            amenities=[a.get("name","") for a in ag.get("amenities",[])]

            lang=extract_language(amenities)
            fmt_final=extract_format(amenities,fmt)

            for show in ag.get("showtimes",[]):

                sid=show.get("id")

                if not sid:
                    continue

                out.append({
                    "showtime_id":sid,
                    "date":show.get("ticketingDate"),
                    "format":fmt_final,
                    "language":lang
                })

    return out


# ================= THEATER SCRAPER =================

def get_theaters(zip_code,date):

    url="https://www.fandango.com/napi/theaterswithshowtimes"

    params={
        "zipCode":zip_code,
        "date":date,
        "page":1,
        "limit":40
    }

    try:

        r=requests.get(url,headers=get_headers2(zip_code,date),params=params,timeout=10)

        if r.status_code==200:
            return r.json()

    except Exception:
        pass

    return {}


def process_zip(args):

    zip_code, date = args

    data = get_theaters(zip_code, date)

    if not data:
        return None

    movies = {}
    theatres = {}
    shows = []

    for theater in data.get("theaters", []):

        theater_id = theater.get("id")

        if theater_id:

            theatres[theater_id] = {
                "n": theater.get("name"),
                "c": theater.get("city"),
                "s": theater.get("state"),
                "z": theater.get("zip"),
                "cn": theater.get("chainName"),
                "cc": theater.get("chainCode")
            }

        for movie in theater.get("movies", []):

            movie_id = movie.get("id")

            if movie_id:

                poster = None

                try:
                    poster = movie["poster"]["size"]["300"]
                except:
                    pass

                movies[movie_id] = {
                    "t": movie.get("title"),
                    "r": movie.get("runtime"),
                    "rt": movie.get("rating"),
                    "rd": movie.get("releaseDate"),
                    "g": movie.get("genres", []),
                    "p": poster
                }

                shows.append([
                    show["showtime_id"],
                    movie_id,
                    theater_id,
                    (
                        show["date"][11:16]
                        if show["date"]
                        else ""
                    ),
                    FORMAT_CODES.get(
                        show["format"],
                        show["format"]
                    ),
                    LANG_CODES.get(
                        show["language"],
                        show["language"]
                    )
                ])

    return {
        "movies": movies,
        "theatres": theatres,
        "shows": shows
    }


def scrape_showtimes(zip_list, date):

    args = [(z, date) for z in zip_list]

    all_movies = {}
    all_theatres = {}

    all_shows = []

    seen_showtimes = set()

    with ProcessPoolExecutor(MAX_WORKERS) as exe:

        futures = [exe.submit(process_zip, a) for a in args]

        for f in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="ZIP scan"
        ):

            try:

                result = f.result()

                if not result:
                    continue

                all_movies.update(
                    result["movies"]
                )

                all_theatres.update(
                    result["theatres"]
                )

                for show in result["shows"]:

                    sid = str(
                        show[0]
                    ).strip()

                    if not sid:
                        continue

                    if sid in seen_showtimes:
                        continue

                    seen_showtimes.add(
                        sid
                    )

                    all_shows.append(show)

            except Exception:
                pass

    return (
        all_movies,
        all_theatres,
        all_shows
    )


# ================= SEATMAP =================

def seatmap_url(showtime_id):
    return f"https://tickets.fandango.com/checkoutapi/showtimes/v2/{showtime_id}/seat-map/"


async def fetch_seat(session,show):

    sid=str(show["showtime_id"])

    try:

        async with session.get(seatmap_url(sid),headers=get_seatmap_headers(),timeout=10) as resp:

            if resp.status!=200:
                show["error"]={"status":resp.status}
                return

            data=await resp.json()

            d=data.get("data",{})

            available=d.get("totalAvailableSeatCount",0)
            total=d.get("totalSeatCount",0)

            sold=total-available

            show["totalSeatSold"]=sold
            show["totalSeatCount"]=total
            show["occupancy"]=round((sold/total)*100,2) if total else 0

            price = 0

            areas = d.get("areas", [])

            # try finding adult price in any area
            for area in areas:
                for t in area.get("ticketInfo", []):
                    if "adult" in t.get("desc", "").lower():
                        try:
                            price = float(t.get("price", 0))
                            break
                        except:
                            pass
                if price:
                    break

            # fallback if adult not found
            if price == 0:
                for area in areas:
                    ti = area.get("ticketInfo", [])
                    if ti:
                        try:
                            price = float(ti[0].get("price", 0))
                            break
                        except:
                            pass

            show["adultTicketPrice"] = price
            show["grossRevenueUSD"] = round(price * sold, 2)

    except Exception as e:

        show["error"]={"exception":str(e)}


async def run_all(shows):

    connector=aiohttp.TCPConnector(ssl=False)

    retry=ExponentialRetry(attempts=3)

    async with RetryClient(connector=connector,retry_options=retry) as session:

        sem=asyncio.Semaphore(CONCURRENCY)

        async def bound(s):
            async with sem:
                await fetch_seat(session,s)

        tasks=[bound(s) for s in shows]

        for f in tqdm(asyncio.as_completed(tasks),total=len(tasks),desc="Seatmaps"):
            await f


# ================= MAIN =================

def run_for_date(RELEASE_DATE):

    now_pst = datetime.now(
        ZoneInfo("America/Los_Angeles")
    ).date()

    DATE = (
        RELEASE_DATE
        if now_pst < RELEASE_DATE
        else now_pst
    ).strftime("%Y-%m-%d")

    print(
        "DATE:",
        DATE
    )

    zipcodes = open(
        ZIP_FILE,
        encoding="utf-8"
    ).read().splitlines()

    movies, theatres, shows = scrape_showtimes(
        zipcodes,
        DATE
    )

    YEAR = DATE[:4]

    BASE_DIR = os.path.join(
        "USA",
        YEAR
    )

    DATA_DIR = os.path.join(
        BASE_DIR,
        "data"
    )

    os.makedirs(
        DATA_DIR,
        exist_ok=True
    )

    movies_file = os.path.join(
        BASE_DIR,
        "movies.json"
    )

    theatres_file = os.path.join(
        BASE_DIR,
        "theatres.json"
    )

    shows_file = os.path.join(
        DATA_DIR,
        f"{DATE}.json"
    )

    failures_file = os.path.join(
        BASE_DIR,
        "failures.json"
    )

    existing_movies = {}

    existing_theatres = {}

    if os.path.exists(movies_file):
        try:
            existing_movies = json.load(
                open(
                    movies_file,
                    encoding="utf-8"
                )
            )
        except:
            pass

    if os.path.exists(theatres_file):
        try:
            existing_theatres = json.load(
                open(
                    theatres_file,
                    encoding="utf-8"
                )
            )
        except:
            pass

    for movie_id, movie_data in movies.items():

        if movie_id not in existing_movies:

            existing_movies[
                movie_id
            ] = movie_data

    for theatre_id, theatre_data in theatres.items():

        if theatre_id not in existing_theatres:

            existing_theatres[
                theatre_id
            ] = theatre_data


    with open(
        movies_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            existing_movies,
            f,
            ensure_ascii=False,
            indent=2
        )

    with open(
        theatres_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            existing_theatres,
            f,
            ensure_ascii=False,
            indent=2
        )

    shows.sort(
        key=lambda x: (
            str(x[1]),
            str(x[2]),
            str(x[3]),
            str(x[0])
        )
    )

    if REWRITE_SHOWS:

        final_shows = shows

    else:

        existing_shows = {}

        if os.path.exists(shows_file):

            try:

                old = json.load(
                    open(
                        shows_file,
                        encoding="utf-8"
                    )
                )

                for s in old:
                    existing_shows[
                        str(
                            s[0]
                        )
                    ] = s

            except:
                pass

        for s in shows:

            existing_shows[
                str(
                    s[0]
                )
            ] = s

        final_shows = list(
            existing_shows.values()
        )

        final_shows.sort(
            key=lambda x: (
                str(x[1]),
                str(x[2]),
                str(x[3]),
                str(x[0])
            )
        )

    with open(
        shows_file,
        "w",
        encoding="utf-8"
    ) as f:

        f.write("[\n")

        for i, row in enumerate(
            final_shows
        ):

            if i:
                f.write(",\n")

            f.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(
                        ",",
                        ":"
                    )
                )
            )

        f.write("\n]")

    json.dump(
        {
            "date": DATE,
            "movies": len(existing_movies),
            "theatres": len(existing_theatres),
            "shows": len(final_shows)
        },
        open(
            os.path.join(
                BASE_DIR,
                "index.json"
            ),            "w",
            encoding="utf-8"
        ),
        separators=(",", ":")
    )

    with open(
        failures_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            [],
            f,
            ensure_ascii=False,
            indent=2
        )

    print(
        f"Movies: {len(existing_movies):,}"
    )

    print(
        f"Theatres: {len(existing_theatres):,}"
    )

    print(
        f"Shows: {len(final_shows):,}"
    )


# ================= ENTRY =================

if __name__ == "__main__":

    run_for_date(
        datetime.now(
            ZoneInfo("America/Los_Angeles")
        ).date()
    )

    print("\nFinished")
