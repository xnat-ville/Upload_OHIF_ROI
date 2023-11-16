__version__ = (0, 1, 0)

import contextlib
import dataclasses
import http
import netrc
import pathlib
import typing
import urllib.parse

import click
import httpx
import magic
import pydicom


@dataclasses.dataclass
class OHIFNamespace:
    host:     str
    files:    typing.Sequence[pathlib.Path]
    username: typing.Optional[str]
    password: typing.Optional[str]
    port:     typing.Optional[int]


# Used to pass the top-level namespace context
# to commands lower than the entry point.
pass_clinamespace = click.make_pass_decorator(OHIFNamespace)


def auth_netrc(namespace: OHIFNamespace) -> tuple[str, str]:
    """
    Attempt to get credentials from `~/.netrc`
    file.
    """

    default = ("", "", "")
    host  = namespace.host
    colon = host.rfind(":")
    if ":" in host and "//" not in host[colon:]:
        host = host[:colon]

    # Single out the hostname.
    host_parsed = urllib.parse.urlparse(host)
    host = host_parsed.netloc or host_parsed.path

    return (netrc.netrc().authenticators(host) or default)[::2]


def dicom_isdicom_file(path: pathlib.Path):
    """
    Given path is a file, exists, and is a DICOM
    file.
    """

    if not path.exists():
        return False
    if path.is_dir():
        return False

    return magic.from_file(str(path)) == "DICOM medical imaging data"


@contextlib.contextmanager
def rest_attempt():
    """
    Catches RESTful exceptions and handles them.
    """

    request_extractor = lambda err: (err.request.method, err.request.url.path)

    try:
        yield
    except httpx.HTTPStatusError as error:
        method, path = request_extractor(error)
        code   = error.response.status_code
        phrase = http.HTTPStatus(code).phrase
        click.echo(
            f"{click.style('error', fg='red')}: "
            f"({method}) {path} failed: <{code} {phrase}>",
            err=True)
        quit(1)
    except httpx.RequestError as error:
        method, path = request_extractor(error)
        click.echo(
            f"{click.style('error', fg='red')}: "
            f"({method}) {path} failed: {error}",
            err=True)
        quit(1)


def rest_auth(namespace: OHIFNamespace) -> httpx.Auth:
    """Parse credentials for the remote XNAT."""

    username, password = auth_netrc(namespace)
    # Allow for user defined override at CLI
    # execution.
    username = namespace.username or username
    password = namespace.password or password
    return httpx.BasicAuth(username, password)


def rest_client(
        namespace: OHIFNamespace,
        *,
        verify: typing.Optional[bool] = None):
    """
    Create a REST client to make calls against the
    remote XNAT.
    """

    client = httpx.Client(
        auth=rest_auth(namespace),
        base_url=rest_host(namespace),
        verify=verify if verify is not None else True)
    return client


def rest_host(namespace: OHIFNamespace) -> str:
    """Parse host base URL."""

    # Weed out the port number
    host  = namespace.host
    colon = host.rfind(":")
    if ":" in host and "//" not in host[colon:]:
        host, port = host[:colon], int(host[colon+1:])
    else:
        host, port = host, -1

    port = namespace.port or port

    # Rotating through the given host argument to
    # ensure the anatomy of the URL is correct.
    parsed = urllib.parse.urlparse(host)
    parsed = urllib.parse.ParseResult(
        parsed.scheme or "https",
        parsed.netloc or parsed.path,
        "",
        parsed.params,
        parsed.query,
        parsed.fragment)

    uri = parsed.geturl()

    if port > 0:
        uri += f":{port}"

    return  uri


def rest_ohif_roi_store(
    namespace: OHIFNamespace,
    project: str,
    session: str,
    *,
    overwrite: typing.Optional[bool] = None):
    """
    Attempt to send a PUT request to XNAT to
    store an ROI collection.
    """

    if not namespace.files:
        click.echo(
            f"{click.style('error', fg='red')}: no files were provided",
            err=True)
        quit(1)

    uri_template = "/".join([
        "",
        "xapi",
        "roi",
        "projects",
        project, # projectId
        "sessions",
        session, # sessionId
        "collections",
        "{label}"
    ])

    rest = rest_client(namespace)

    headers = dict()
    headers["Content-Type"] = "application/octet-stream"

    params = dict()
    params["overwrite"] = str(overwrite or False).lower()
    params["seriesuid"] = None #type: ignore[assignment]
    params["type"]      = None #type: ignore[assignment]

    with contextlib.ExitStack() as es:
        es.enter_context(rest_attempt())
        files = ((f, es.enter_context(f.open("rb"))) for f in namespace.files)

        for file, fd in files:
            dcm_data = pydicom.dcmread(file)
            uri = uri_template.format(
                label=dcm_data[(0x0008, 0x103e)].value.replace(" ", "_"))

            # Assign parameters based on headers
            # found in DICOM.
            rparams  = params.copy()
            rparams["seriesuid"] = dcm_data[(0x0020, 0x000e)].value
            rparams["type"]      = dcm_data[(0x0008, 0x0060)].value

            r = rest.put(uri, data=fd, headers=headers, params=rparams)
            r.raise_for_status()


def rest_username(namespace: OHIFNamespace) -> str:
    """Get the REST username."""

    rest = rest_client(namespace)
    with rest_attempt():
        r = rest.get("/xapi/users/username")
        r.raise_for_status()

    return r.text


@click.group()
@click.pass_context
@click.option("--host", "-h")
@click.option("--username", "-u", default=None)
@click.option("--password", "-p", default=None)
@click.option("--port", "-P", type=int, default=None)
def ohif(
    ctx: click.Context,
    *,
    host: str,
    username: typing.Optional[str],
    password: typing.Optional[str],
    port: typing.Optional[int]):
    """Manage OHIF via XNAT."""

    ctx.obj = OHIFNamespace(host, (), username, password, port)
    # Validate that credentials are valid by first
    # making an attempt to get their username.
    rest_username(ctx.obj)

    return 0


@ohif.group()
def roi():
    """OHIF ROI Management."""


@roi.command
@pass_clinamespace
@click.argument("project")
@click.argument("session")
@click.option("--file", "-f", "files", type=pathlib.Path, multiple=True)
@click.option(
    "--overwrite/--create",
    "-O/",
    "overwrite",
    help="create or overwrite collection")
def store(
    namespace: OHIFNamespace,
    project: str,
    session: str,
    *,
    files: typing.Sequence[pathlib.Path],
    overwrite: bool):
    """Store an ROI collection."""

    # Filter out directories from files. Replace
    # directories with files found in subtree.
    dirs  = filter(lambda p: p.is_dir(), files)
    files = [p for p in files if not p.is_dir()]

    for path in dirs:
        for dpath, _, dpath_files in path.walk():
            if not dpath_files:
                continue
            # Filter out non-DICOM files
            files_map = map(dpath.joinpath, dpath_files)
            files.extend(filter(dicom_isdicom_file, files_map))

    namespace.files = files
    rest_ohif_roi_store(
        namespace,
        project,
        session,
        overwrite=overwrite)


def from_command_line():
    return ohif()


if __name__ == "__main__":
    exit(from_command_line())
