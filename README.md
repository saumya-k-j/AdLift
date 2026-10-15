# Penn Labs Backend Challenge

## Documentation

### The data

Before writing the models I checked what was actually in `clubs.json`: 5 clubs,
4 keys each (`code`, `name`, `description`, `tags`), no missing values or type
inconsistencies, `code` unique across all records. 12 tag mentions but only 7
distinct tags, with "Undergraduate" on 4 of the 5 clubs.

### Data model

Three tables (`Club`, `Tag`, `User`) plus two association tables (`club_tags`,
`club_favorites`).

**Surrogate primary keys.** Each club already has a unique `code`, so it could
have been the primary key. I used an integer id and kept `code` as a unique
indexed column instead, because `code` is an external identifier: it appears in
URLs and clients may have saved it. If a club rebrands and the code changes, a
natural key means that change cascades into every referencing row. With a
surrogate key it is one UPDATE. This is arguable at this size and `code` as the
primary key would have worked; the cost of the surrogate is one column.

**Tags as their own table.** The alternative is a JSON or comma-separated
column on `Club`, which is simpler and maps directly onto the input JSON. I
normalized because:

- A tag is shared across clubs rather than owned by one. "Undergraduate" exists
  independently of any club claiming it.
- A unique constraint on `Tag.name` prevents "Tech" and "Technology" both
  entering the vocabulary. A string column has nothing stopping that, and bad
  data costs more to fix than a slow query.
- Tag-level questions stay as SQL. `/api/tags` is one GROUP BY. Against a
  string column it means reading every club and tallying in Python, and
  `LIKE '%Tech%'` would also match "Educational Technology".

If tags were free-form per-club text with no shared vocabulary, the string
column would be the better choice.

**Plain association tables.** `club_tags` and `club_favorites` are `db.Table`
objects rather than model classes, since neither relationship carries data of
its own. If one needed an attribute (when a club was favorited, who applied a
tag) it would become an association object with a model class. Favorites are
the borderline case, since a timestamp would be reasonable to want, but nothing
in the spec needs one.

`club_favorites` has a composite primary key over `(user_id, club_id)`, so a
duplicate favorite is impossible in the database rather than being an
application check that could race.

**User fields.** The challenge gives no user data beyond the name, so I picked
fields with a use: `username` as the public handle and URL segment, `full_name`
for display, `email` as contact and eventual login identifier,
`graduation_year` and `major` as profile context for filtering or sorting
reviews later.

No password or credential column. Auth is out of scope here and covered in
`WRITEUP.md` Q1.

### Routes

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/clubs` | All clubs. `?search=<str>` filters by name, case-insensitive. |
| POST | `/api/clubs` | Create a club from `{code, name, description?, tags?}`. |
| GET | `/api/clubs/<code>` | A single club. |
| PATCH | `/api/clubs/<code>` | Update any subset of `{name, description, tags}`. |
| POST | `/api/clubs/<code>/favorite` | Favorite a club on behalf of `{username}`. |
| GET | `/api/users/<username>` | Public profile for a user. |
| GET | `/api/tags` | Every tag with the number of clubs carrying it. |

`GET /api/clubs/<code>` is not in the spec. I added it because POST returns a
`Location` header pointing at the new club, and a collection endpoint with no
item endpoint is awkward to consume.

**Search as a query parameter.** `?search=juggling` rather than a
`/api/clubs/search/<query>` path. Search returns a filtered view of the same
collection, not a different resource, and filters belong in the query string.
It also composes: a `?tag=` filter could be added later and combined with
`?search=`.

The filter runs in SQL via `ilike` rather than loading every club and filtering
in Python. LIKE wildcards in the input are escaped, so searching for `100%`
matches literally instead of becoming a pattern that matches everything.

A caveat: a leading-wildcard `LIKE '%term%'` cannot use a B-tree index, so the
index on `name` helps prefix matches and not infix ones. If this route were hot
the answer would be a full-text index (SQLite FTS5, or `tsvector` with GIN on
Postgres).

**PATCH rather than PUT.** PUT replaces the whole resource, so the client sends
every field and an omitted field means clear it. Editing one description would
mean resending name and tags to avoid wiping them. PATCH applies only what is
present.

`code` is immutable. It is the identifier in the URL and in favorite rows, so
changing it breaks saved links and any client holding the old value. An attempt
to change it returns 400 rather than being ignored, since a client that thinks
it renamed a club and got a 200 has been misled. Unknown fields are rejected
for the same reason: `descriptoin` should error, not silently do nothing.

Within a PATCH, `tags` is the complete new list for that field. Assignment
replaces the set, which is simpler than add/remove semantics on a subfield.

**Favoriting.** Modeled as an action on a club rather than a top-level
`/api/favorites` resource. Both work; this keeps the club code in the path
where it identifies the thing being acted on. The tradeoff is that unfavoriting
becomes a DELETE on the same path rather than on a favorite's own id.

The username comes from the request body because there is no auth and so no
session to infer an actor from. As written, anyone can favorite on anyone's
behalf. This is a stand-in for a session identity, not a design choice.

The operation is idempotent: favoriting twice returns 200 with the unchanged
state and `"created": false`. A double click is not worth failing, and the
composite primary key already rules out a duplicate row.

**The user profile omits email.** The endpoint is unauthenticated, so it cannot
tell the owner apart from any other caller and has to return what is safe for
the least trusted one. Email is contact information and nothing on a club
review site needs it.

`User.to_dict()` takes an `include_private` flag defaulting to `False` rather
than the column being dropped, so the model stays the one place that knows
which fields are sensitive. Once auth exists an owner-facing route flips the
flag.

Favorites are included, since who endorses a club is the point of the site.
Only each club's code and name, as full club objects would duplicate
`/api/clubs`.

### Error handling

All errors return `{"error": "..."}`, including 404 and 405. Flask's defaults
for those are HTML, which breaks clients calling `response.json()`.

- **400** for a malformed or invalid body: missing required field, wrong type,
  attempt to change `code`, unrecognized field.
- **404** when a club or user in the path does not exist.
- **405** when the path exists but the method does not.
- **409** when creating a club whose code is taken. The request is well formed,
  so it is a conflict with state rather than a client error in the body.
- **201** with a `Location` header on creation.

Required fields are validated in the handler rather than by the database, so a
missing `name` gives a 400 instead of an `IntegrityError` surfacing as a 500.

### Known weaknesses

**`favorite_count` counts a loaded relationship.** `Club.to_dict()` uses
`len(self.favorited_by)`. With `lazy="selectin"` this is one extra query for
the whole result set rather than an N+1, but it loads rows purely to count
them. At scale this should be a GROUP BY aggregate like `/api/tags` uses, or a
counter column on `Club` if reads dominate writes.

**Tag creation is implicit.** POSTing or PATCHing a club with an unknown tag
name creates it, so a typo quietly adds to the shared vocabulary. A moderated
system would reject unknown tags or flag new ones as pending.

**No pagination on `/api/clubs`.** Limit and offset would be the first
addition.

**No authentication.** Covered in `WRITEUP.md` Q1. Everything else in this API
is shaped by its absence.

### Tests

`poetry run pytest` runs 37 tests in `test_app.py` covering every route and
every error branch: search behaviour including wildcard escaping, PATCH
rejecting code changes and unknown fields, favoriting being idempotent, the
user profile omitting email, tags with zero clubs still appearing, and errors
returning JSON rather than HTML.

Each test runs against a fresh temporary SQLite database created by the
`client` fixture, so the suite never touches the development database. The
database URI in `app.py` reads from a `DATABASE_URL` environment variable for
that reason, defaulting to the local file.

## Installation

1. Click the green "use this template" button to make your own copy of this repository, and clone it. Make sure to create a **private repository**.
2. Change directory into the cloned repository.
3. Install `pipx`
   - `brew install pipx` (macOS)
   - See instructions here https://github.com/pypa/pipx for other operating systems
4. Install `poetry`
   - `pipx install poetry`
5. Install packages using `poetry install --no-root`.

### Notes for running on macOS

**Python version.** SQLAlchemy 2.0.25, pinned in the committed lockfile, does
not import on Python 3.13+. Python 3.13 added `__firstlineno__` and
`__static_attributes__` to every class, and SQLAlchemy asserts that its
`TypingOnly` classes have no extra attributes, so the import fails on that
assertion. If your default interpreter is 3.13+, point Poetry at an older one
instead of editing the lockfile:

```
poetry env use /usr/bin/python3
```

**Port 5000.** macOS AirPlay Receiver listens on port 5000. `localhost`
resolves to IPv6 first while Flask binds IPv4, so requests to `localhost:5000`
get a 403 from AirPlay and never reach Flask. Use `curl 127.0.0.1:5000/...` or
run on another port:

```
poetry run flask run --port 5001
```

## File Structure

- `app.py`: Main file. Has configuration and setup at the top. Add your [URL routes](https://flask.palletsprojects.com/en/1.1.x/quickstart/#routing) to this file!
- `models.py`: Model definitions for SQLAlchemy database models. Check out documentation on [declaring models](https://flask-sqlalchemy.palletsprojects.com/en/2.x/models/) as well as the [SQLAlchemy quickstart](https://flask-sqlalchemy.palletsprojects.com/en/2.x/quickstart/#quickstart) for guidance
- `bootstrap.py`: Code for creating and populating your local database. You will be adding code in this file to load the provided `clubs.json` file into a database.

## Developing

0. Determine how to model the data contained within `clubs.json` and then complete `bootstrap.py`
1. Activate the Poetry shell with `poetry shell`.
2. Run `python3 bootstrap.py` to create the database and populate it.
3. Use `flask run` to run the project.
4. Follow the instructions [here](https://www.notion.so/pennlabs/Backend-Challenge-862656cb8b7048db95aaa4e2935b77e5).
5. Document your work in this `README.md` file.

## Submitting

Follow the instructions on the Technical Challenge page for submission.

## Installing Additional Packages

Use any tools you think are relevant to the challenge! To install additional packages
run `poetry add <package_name>` within the directory. Make sure to document your additions.
