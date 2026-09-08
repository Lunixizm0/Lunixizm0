import hashlib
import os
import time

import requests
from lxml import etree  # type: ignore[reportAttributeAccessIssue]

try: 
    import dotenv
    dotenv.load_dotenv()
except ImportError:
    pass

class GitHubAPIError(Exception):
    pass

# Fine-grained personal access token with All Repositories access:
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME'] # 'Lunixizm0'
QUERY_COUNT = {'user_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'loc_query': 0, 'issues_getter': 0, 'projects_getter': 0}

PROJECT_NAMES = ['Storefront-Research', 'CopySec', 'firebase-dumper', 'linux-autoruns']


def simple_request(func_name, query, variables):
    #Returns a request, or raises an Exception if the response does not succeed.
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise GitHubAPIError(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    #Uses GitHub's GraphQL v4 API to return my total commit count
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(owner_affiliation):
    #Uses GitHub's GraphQL v4 API to return my total repository count
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!) {
        user(login: $login) {
            repositories(ownerAffiliations: $owner_affiliation) {
                totalCount
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        return request.json()['data']['user']['repositories']['totalCount']


def recursive_loc(owner, repo_name, data, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    #Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
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
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS) # I cannot use simple_request(), because I want to save the file before raising Exception
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] != None: # Only count commits if repo isn't empty
            return loc_counter_one_repo(owner, repo_name, data, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else: return 0, 0, 0
    force_close_file(data) # saves what is currently in the file before this program crashes
    if request.status_code == 403:
        raise GitHubAPIError('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise GitHubAPIError('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, history, addition_total, deletion_total, my_commits):
    #Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    #only adds the LOC value of commits authored by me
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, force_cache=False, cursor=None, edges=None):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
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
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']            # Add on to the LoC count
        return loc_query(owner_affiliation, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], force_cache)


def cache_builder(edges, force_cache, loc_add=0, loc_del=0):
    #Checks each repository in edges to see if it has been updated since the last time it was cached
    #If it has, run recursive_loc on that repository to update the LOC count
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename)
        with open(filename, 'r') as f:
            data = f.readlines()

    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError: # If the repo is empty
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename):
    with open(filename, 'w') as f:
        f.writelines(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n' for node in edges)


def force_close_file(data):
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(data)


def svg_overwrite(filename, commit_data, repo_data, contrib_data, issue_data, loc_data, projects):
    TARGET = 63
    tree = etree.parse(filename)
    root = tree.getroot()
    # Repos / Contributed
    repo = f'{int(repo_data):,}'
    contrib = f'{int(contrib_data):,}'
    find_and_replace(root, 'repo_data', repo)
    find_and_replace(root, 'contrib_data', contrib)
    repo_suffix = f' {{Contributed: {contrib}}}'
    set_dots(root, 'repo_data_dots', TARGET - 8 - len(repo) - len(repo_suffix))
    # Commits / Issues
    commit = f'{int(commit_data):,}'
    issue_total, issue_open, issue_closed = map(int, issue_data[:3])
    find_and_replace(root, 'commit_data', commit)
    find_and_replace(root, 'issue_data', f'{issue_total:,}')
    find_and_replace(root, 'issue_open', f'{issue_open:,}')
    find_and_replace(root, 'issue_closed', f'{issue_closed:,}')
    issue_dots = ' . ' if issue_total else ' '
    issue_suffix = f' | Issues:{issue_dots}{issue_total:,} {{Open: {issue_open:,}, Closed: {issue_closed:,}}}'
    set_dots(root, 'commit_data_dots', TARGET - 10 - len(commit) - len(issue_suffix))
    # Lines of Code
    loc_add, loc_del, loc_total = loc_data
    find_and_replace(root, 'loc_data', loc_total)
    find_and_replace(root, 'loc_add', loc_add)
    find_and_replace(root, 'loc_del', loc_del)
    loc_suffix = f' ( {loc_add}++,  {loc_del}-- )'
    set_dots(root, 'loc_data_dots', TARGET - 26 - len(loc_total) - len(loc_suffix))
    # Projects / Descriptions
    for index, (name, desc) in enumerate(projects, start=1):
        desc = (desc or '').strip()
        max_desc = TARGET - 6 - len(name)
        if desc and len(desc) > max_desc:
            desc = desc[:max_desc-1] + '\u2026'
        elif len(desc) > max_desc:
            desc = desc[:max(0, max_desc)]
        find_and_replace(root, f'project{index}', desc)
        available = max(3, TARGET - 3 - len(name) - len(desc))
        set_dots(root, f'project{index}', available)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def set_dots(root, element_id, available):
    available = max(0, available)
    dot_map = {0: '', 1: ' ', 2: '. '}
    if available <= 2:
        dot_string = dot_map[available]
    else:
        dot_string = ' ' + ('.' * (available - 2)) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter():
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def issues_getter(username):
    query_count('issues_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            allIssues: issues { totalCount }
            openIssues: issues(filterBy: {states: [OPEN]}) { totalCount }
            closedIssues: issues(filterBy: {states: [CLOSED]}) { totalCount }
        }
    }'''
    request = simple_request(issues_getter.__name__, query, {'login': username})
    data = request.json()['data']['user']
    return data['allIssues']['totalCount'], data['openIssues']['totalCount'], data['closedIssues']['totalCount']


def projects_getter(username):
    query_count('projects_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            repositories(first: 100, ownerAffiliations: [OWNER]) {
                edges {
                    node {
                        ... on Repository {
                            name
                            description
                        }
                    }
                }
            }
        }
    }'''
    request = simple_request(projects_getter.__name__, query, {'login': username})
    repos = {}
    for edge in request.json()['data']['user']['repositories']['edges']:
        node = edge['node']
        repos[node['name']] = (node['description'] or '').strip()
    return [(name, repos.get(name, '')) for name in PROJECT_NAMES]


def query_count(funct_id):
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    print(f"{'   ' + query_type + ':':<23}", end='')
    if difference > 1:
        print(f"{f'{difference:.4f} s ':>12}")
    else:
        print(f"{f'{difference * 1000:.4f} ms ':>12}")
    if whitespace:
        return f"{f'{funct_return:,}': <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter)
    repo_data, repo_time = perf_counter(graph_repos_stars, ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    issue_data, issue_time = perf_counter(issues_getter, USER_NAME)
    projects_data, projects_time = perf_counter(projects_getter, USER_NAME)
    formatter('projects', projects_time)

    for index in range(len(total_loc)-1): total_loc[index] = f'{total_loc[index]:,}' # format added, deleted, and total LOC

    svg_overwrite('dark_mode.svg', commit_data, repo_data, contrib_data, issue_data, total_loc[:-1], projects_data)
    svg_overwrite('light_mode.svg', commit_data, repo_data, contrib_data, issue_data, total_loc[:-1], projects_data)

    # move cursor to override 'Calculation times:' with 'Total function time:' and the total function time, then move cursor back
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
        f"{'Total function time:':<21}", f'{user_time + loc_time + commit_time + repo_time + contrib_time + issue_time + projects_time:>11.4f}',
        ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', f'{sum(QUERY_COUNT.values()):>3}')
    for funct_name, count in QUERY_COUNT.items(): print(f"{'   ' + funct_name + ':':<28}", f'{count:>6}')