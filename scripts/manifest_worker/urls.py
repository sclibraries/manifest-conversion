"""Source URL identity shared by intake, discovery and the Sheet queue."""
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from .core import https_url

ASPACE_HOSTS = ('findingaids.smith.edu', 'archives.example.org')
COMPASS_HOST = 'compass.fivecolleges.edu'


def source_url(value):
    value = value.strip()
    if any(value.startswith(host + '/') for host in (*ASPACE_HOSTS, COMPASS_HOST)):
        value = 'https://' + value
    parsed = urlsplit(value)
    parsed = https_url(urlunsplit(parsed._replace(fragment='')))
    if parsed.query:
        raise ValueError('Source URL must not have query parameters')
    path = quote(unquote(parsed.path).rstrip('/'), safe='/:@-._~')
    return urlunsplit(('https', parsed.hostname.lower(), path, '', ''))


def normalized_source(value):
    parsed = urlsplit(source_url(value))
    path = unquote(parsed.path)
    if parsed.hostname == COMPASS_HOST and path.startswith('/object/'):
        path = '/islandora' + path
    return parsed.hostname + path
