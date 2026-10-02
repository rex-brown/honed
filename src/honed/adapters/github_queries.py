"""GitHub GraphQL documents. GitHub charges by requested page sizes (not results), so every nested page size is a
variable the client can shrink after a timeout and the caller can size to the expected count."""

RATE = "rateLimit { cost remaining resetAt }"

SEARCH_PRS = f"""
query($q: String!, $n: Int!, $after: String, $byCommit: Boolean!) {{
  {RATE}
  search(query: $q, type: ISSUE, first: $n, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{
      number title createdAt baseRefName
      author {{ login __typename }}
      reviewThreads {{ totalCount }}
      reviews(first: 20) {{ nodes {{ author {{ login __typename }} }} }}
      changesRequested: reviews(first: 10, states: [CHANGES_REQUESTED]) {{ nodes {{ author {{ login __typename }} }} }}
      closed: timelineItems(itemTypes: [CLOSED_EVENT], last: 1) @include(if: $byCommit) {{
        nodes {{ ... on ClosedEvent {{ closer {{ __typename }} }} }}
      }}
    }} }}
  }}
}}"""

_COMMENT = """
fragment CommentFields on PullRequestReviewComment {
  id body createdAt diffHunk line originalLine startLine originalStartLine
  author { login __typename }
  commit { oid }
  originalCommit { oid }
  reactions(first: $reactions) { totalCount nodes { content user { login } } }
}"""

_THREAD = """
fragment ThreadFields on PullRequestReviewThread {
  id path isResolved isOutdated diffSide subjectType line originalLine startLine originalStartLine
  resolvedBy { login }
  comments(first: $comments) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes { ...CommentFields }
  }
}"""

PULL_REQUEST = f"""
query($owner: String!, $name: String!, $number: Int!, $threads: Int!, $comments: Int!, $reactions: Int!) {{
  {RATE}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      number title url body createdAt mergedAt closedAt baseRefName baseRefOid headRefOid
      additions deletions changedFiles
      author {{ login __typename }}
      forcePushes: timelineItems(itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {{ filteredCount }}
      reviews(first: 100) {{ nodes {{ id state submittedAt author {{ login __typename }} commit {{ oid }} }} }}
      commits(first: 100) {{ totalCount nodes {{ commit {{ oid committedDate authoredDate messageHeadline }} }} }}
      reviewThreads(first: $threads) {{
        totalCount
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ ...ThreadFields }}
      }}
    }}
  }}
}}
{_THREAD}
{_COMMENT}"""

MORE_THREADS = f"""
query($owner: String!, $name: String!, $number: Int!, $threads: Int!, $after: String!, $comments: Int!,
      $reactions: Int!) {{
  {RATE}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      reviewThreads(first: $threads, after: $after) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ ...ThreadFields }}
      }}
    }}
  }}
}}
{_THREAD}
{_COMMENT}"""

MORE_COMMENTS = f"""
query($id: ID!, $comments: Int!, $after: String!, $reactions: Int!) {{
  {RATE}
  node(id: $id) {{ ... on PullRequestReviewThread {{
    comments(first: $comments, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ ...CommentFields }}
    }}
  }} }}
}}
{_COMMENT}"""


# A stripped bundle's text, by id (`honed rehydrate --comments`): up to 100 review comments per query (1 point).
COMMENT_BODIES = f"""
query($ids: [ID!]!) {{
  {RATE}
  nodes(ids: $ids) {{ ... on PullRequestReviewComment {{ id body }} }}
}}"""


def pr_bodies(numbers: list[int]) -> str:
    """One repository's PR descriptions, one aliased field per PR."""
    fields = "\n    ".join(f"pr{n}: pullRequest(number: {int(n)}) {{ body }}" for n in numbers)
    return f"""
query($owner: String!, $name: String!) {{
  {RATE}
  repository(owner: $owner, name: $name) {{
    {fields}
  }}
}}"""
