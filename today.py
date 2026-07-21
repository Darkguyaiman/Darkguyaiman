import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
USER_NAME = os.environ.get("USER_NAME", "Darkguyaiman")
HEADERS = {
    "Authorization": "Bearer " + os.environ["ACCESS_TOKEN"],
    "Content-Type": "application/json",
    "User-Agent": "Darkguyaiman-readme-builder",
}
AFFILIATIONS = ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
CACHE_COMMENT_SIZE = 7
QUERY_COUNT = {
    "overview": 0,
    "loc_query": 0,
    "recursive_loc": 0,
}


def format_plural(unit):
    return "s" if unit != 1 else ""


def add_months(value, months):
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    next_month = dt.date(year + month // 12, month % 12 + 1, 1)
    last_day = (next_month - dt.timedelta(days=1)).day
    return dt.date(year, month, min(value.day, last_day))


def daily_readme(birthday):
    today = dt.date.today()
    birthday = birthday.date() if isinstance(birthday, dt.datetime) else birthday

    years = today.year - birthday.year
    if birthday.replace(year=today.year) > today:
        years -= 1

    after_years = birthday.replace(year=birthday.year + years)
    months = (today.year - after_years.year) * 12 + today.month - after_years.month
    if add_months(after_years, months) > today:
        months -= 1

    after_months = add_months(after_years, months)
    days = (today - after_months).days
    cake = " 🎂" if months == 0 and days == 0 else ""
    return (
        f"{years} year{format_plural(years)}, "
        f"{months} month{format_plural(months)}, "
        f"{days} day{format_plural(days)}{cake}"
    )


def github_graphql(func_name, query, variables):
    query_count(func_name)
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GITHUB_GRAPHQL_URL,
        data=payload,
        headers=HEADERS,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{func_name} failed with HTTP {exc.code}: {body}; query counts: {QUERY_COUNT}"
        ) from exc

    parsed = json.loads(body)
    if parsed.get("errors"):
        raise RuntimeError(f"{func_name} returned GraphQL errors: {parsed['errors']}")
    return parsed["data"]


def overview_query():
    query = """
    query($login: String!, $affiliations: [RepositoryAffiliation]) {
      user(login: $login) {
        id
        createdAt
        followers {
          totalCount
        }
        ownerRepositories: repositories(ownerAffiliations: [OWNER]) {
          totalCount
        }
        contributedRepositories: repositories(ownerAffiliations: $affiliations) {
          totalCount
        }
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                contributionCount
                date
              }
            }
          }
        }
      }
    }"""
    data = github_graphql(
        "overview",
        query,
        {"login": USER_NAME, "affiliations": AFFILIATIONS},
    )
    user = data["user"]
    calendar = user["contributionsCollection"]["contributionCalendar"]
    return {
        "owner_id": user["id"],
        "created_at": user["createdAt"],
        "followers": int(user["followers"]["totalCount"]),
        "repos": int(user["ownerRepositories"]["totalCount"]),
        "contributed_repos": int(user["contributedRepositories"]["totalCount"]),
        "contributions": int(calendar["totalContributions"]),
        "streak": current_streak(calendar["weeks"]),
    }


def current_streak(weeks):
    days = [
        day
        for week in weeks
        for day in week["contributionDays"]
    ]
    days.sort(key=lambda item: item["date"], reverse=True)
    if not days:
        return 0

    start_index = 0
    if days[0]["contributionCount"] == 0:
        if len(days) > 1 and days[1]["contributionCount"] > 0:
            start_index = 1
        else:
            return 0

    streak = 0
    for day in days[start_index:]:
        if day["contributionCount"] == 0:
            break
        streak += 1
    return streak


def recursive_loc(owner, repo_name, owner_id, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query = """
    query($repo_name: String!, $owner: String!, $cursor: String) {
      repository(name: $repo_name, owner: $owner) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                edges {
                  node {
                    author {
                      user {
                        id
                      }
                    }
                    deletions
                    additions
                  }
                }
                pageInfo {
                  endCursor
                  hasNextPage
                }
              }
            }
          }
        }
      }
    }"""
    try:
        response = github_graphql(
            "recursive_loc",
            query,
            {"repo_name": repo_name, "owner": owner, "cursor": cursor},
        )
    except Exception:
        force_close_file(data, cache_comment)
        raise

    default_branch = response["repository"]["defaultBranchRef"]
    if default_branch is None:
        return 0, 0, 0
    return loc_counter_one_repo(
        owner,
        repo_name,
        owner_id,
        data,
        cache_comment,
        default_branch["target"]["history"],
        addition_total,
        deletion_total,
        my_commits,
    )


def loc_counter_one_repo(owner, repo_name, owner_id, data, cache_comment, history, addition_total, deletion_total, my_commits):
    for edge in history["edges"]:
        node = edge["node"]
        author = node["author"]["user"]
        if author and author["id"] == owner_id:
            my_commits += 1
            addition_total += node["additions"]
            deletion_total += node["deletions"]

    if not history["edges"] or not history["pageInfo"]["hasNextPage"]:
        return addition_total, deletion_total, my_commits
    return recursive_loc(
        owner,
        repo_name,
        owner_id,
        data,
        cache_comment,
        addition_total,
        deletion_total,
        my_commits,
        history["pageInfo"]["endCursor"],
    )


def loc_query(owner_id, owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    edges = [] if edges is None else edges
    query = """
    query($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
          edges {
            node {
              nameWithOwner
              defaultBranchRef {
                target {
                  ... on Commit {
                    history {
                      totalCount
                    }
                  }
                }
              }
            }
          }
          pageInfo {
            endCursor
            hasNextPage
          }
        }
      }
    }"""
    data = github_graphql(
        "loc_query",
        query,
        {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor},
    )
    repositories = data["user"]["repositories"]
    edges.extend(repositories["edges"])
    if repositories["pageInfo"]["hasNextPage"]:
        return loc_query(
            owner_id,
            owner_affiliation,
            comment_size,
            force_cache,
            repositories["pageInfo"]["endCursor"],
            edges,
        )
    return cache_builder(edges, owner_id, comment_size, force_cache)


def cache_builder(edges, owner_id, comment_size, force_cache, loc_add=0, loc_del=0):
    fully_cached = True
    filename = Path("cache") / f"{hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()}.txt"
    filename.parent.mkdir(exist_ok=True)

    try:
        data = filename.read_text().splitlines(keepends=True)
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            data = ["This line is a comment block. Write whatever you want here.\n"] * comment_size
        filename.write_text("".join(data))

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    cache_by_hash = {
        line.split()[0]: line
        for line in data
        if len(line.split()) >= 5
    }

    if len(cache_by_hash) != len(edges) or force_cache:
        fully_cached = False

    updated = []
    for edge in edges:
        repo = edge["node"]
        repo_hash = hashlib.sha256(repo["nameWithOwner"].encode("utf-8")).hexdigest()
        cached_line = cache_by_hash.get(repo_hash)
        commit_count = repo_commit_count(repo)

        if cached_line and not force_cache:
            parts = cached_line.split()
            if int(parts[1]) == commit_count:
                updated.append(cached_line)
                continue

        fully_cached = False
        if commit_count == 0:
            updated.append(f"{repo_hash} 0 0 0 0\n")
            continue

        owner, repo_name = repo["nameWithOwner"].split("/", 1)
        additions, deletions, my_commits = recursive_loc(
            owner,
            repo_name,
            owner_id,
            updated,
            cache_comment,
        )
        updated.append(f"{repo_hash} {commit_count} {my_commits} {additions} {deletions}\n")

    filename.write_text("".join(cache_comment + updated))
    for line in updated:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, fully_cached]


def repo_commit_count(repo):
    try:
        return int(repo["defaultBranchRef"]["target"]["history"]["totalCount"])
    except TypeError:
        return 0


def force_close_file(data, cache_comment):
    filename = Path("cache") / f"{hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()}.txt"
    filename.write_text("".join(cache_comment + data))
    print(f"There was an error while writing to the cache file. Partial data was saved to {filename}.")


def svg_overwrite(filename, age_data, commit_data, streak_data, repo_data, contrib_data, follower_data, loc_data):
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree = ET.parse(filename)
    root = tree.getroot()
    justify_format(root, "age_data", age_data, 48)
    justify_format(root, "commit_data", commit_data, 22)
    justify_format(root, "streak_data", streak_data, 13)
    justify_format(root, "repo_data", repo_data, 6)
    justify_format(root, "contrib_data", contrib_data)
    justify_format(root, "follower_data", follower_data, 10)
    justify_format(root, "loc_data", loc_data[2], 9)
    justify_format(root, "loc_add", loc_data[0])
    justify_format(root, "loc_del", loc_data[1], 7)
    tree.write(filename, encoding="utf-8", xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_string = {0: "", 1: " ", 2: ". "}[just_len]
    else:
        dot_string = " " + ("." * just_len) + " "
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    filename = Path("cache") / f"{hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()}.txt"
    lines = filename.read_text().splitlines()[comment_size:]
    return sum(int(line.split()[2]) for line in lines if line.split())


def query_count(funct_id):
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference):
    print(f"   {query_type + ':':<20}", end="")
    if difference > 1:
        print(f"{difference:>11.4f} s ")
    else:
        print(f"{difference * 1000:>11.4f} ms")


if __name__ == "__main__":
    print("Calculation times:")

    overview, overview_time = perf_counter(overview_query)
    formatter("overview data", overview_time)

    age_data, age_time = perf_counter(daily_readme, dt.date(2008, 1, 1))
    formatter("age calculation", age_time)

    total_loc, loc_time = perf_counter(
        loc_query,
        overview["owner_id"],
        AFFILIATIONS,
        CACHE_COMMENT_SIZE,
    )
    formatter("LOC cached" if total_loc[-1] else "LOC no cache", loc_time)

    commit_data, commit_time = perf_counter(commit_counter, CACHE_COMMENT_SIZE)
    formatter("commit count", commit_time)

    streak_data = f"{overview['streak']} day{format_plural(overview['streak'])}"
    formatted_loc = [f"{value:,}" for value in total_loc[:-1]]
    svg_overwrite(
        "dark_mode.svg",
        age_data,
        commit_data,
        streak_data,
        overview["repos"],
        overview["contributed_repos"],
        overview["followers"],
        formatted_loc,
    )
    svg_overwrite(
        "light_mode.svg",
        age_data,
        commit_data,
        streak_data,
        overview["repos"],
        overview["contributed_repos"],
        overview["followers"],
        formatted_loc,
    )

    total_time = overview_time + age_time + loc_time + commit_time
    print(f"Total function time: {total_time:.4f} s")
    print(f"Total GitHub GraphQL API calls: {sum(QUERY_COUNT.values()):>3}")
    for funct_name, count in QUERY_COUNT.items():
        print(f"   {funct_name + ':':<25} {count:>6}")
