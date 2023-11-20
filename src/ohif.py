__version__ = (0, 1, 0)

import contextlib
import dataclasses
import enum
import functools
import http
import netrc
import pathlib
import shutil
import tempfile
import typing
import urllib.parse

import click
import httpx
import magic
import pydicom
import pydicom.tag


class ROIType(enum.StrEnum):
    """OHIF ROI supported DICOM types."""

    _header_: str | tuple[int, int]
    _modal_: str | typing.Sequence[str]

    @property
    def header(self):
        """DICOM header name or tag."""

        return self._header_

    @property
    def modal(self):
        """
        Modality or sequence of modalities
        compatible with this ROI type.
        """

        return self._modal_

    def __new__(cls, value, modal="", header="SOPClassUID"):
        obj = str.__new__(cls, value)
        obj._value_  = value
        obj._header_ = header
        obj._modal_  = modal

        return obj

    Segmentation_Storage = "1.2.840.10008.5.1.4.1.1.66.4", "SEG"
    SEG = Segmentation_Storage


@dataclasses.dataclass
class OHIFNamespace:
    host:     str
    files:    typing.Sequence[pathlib.Path]
    username: typing.Optional[str]
    password: typing.Optional[str]
    port:     typing.Optional[int]
    verbose:  int


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


def dicom_find_files(*paths: pathlib.Path, strict: bool | None = None) -> typing.Sequence[pathlib.Path]:
    """
    Find all files in given directories. Validate
    that files are DICOM image files.
    """

    files = []

    for path in paths:
        if not path.is_dir() and dicom_isdicom_file(path):
            files.append(path)
            continue
        elif not path.is_dir() and strict in (True, None):
            raise ValueError(f"{path!r} is not a valid DICOM image file.")

        for dpath, _, dpath_files in path.walk():
            if not dpath_files:
                continue
            files.extend(dicom_find_files(
                *map(dpath.joinpath, dpath_files),
                strict=False))

    return tuple(files)


T = typing.TypeVar("T")
@typing.overload
def dicom_get(
    path: pathlib.Path,
    key: tuple[int, int]) -> pydicom.DataElement:
    pass
@typing.overload
def dicom_get(
    path: pathlib.Path, key: tuple[int, int],
    default: T) -> pydicom.DataElement | T:
    pass
@typing.overload
def dicom_get(path: pathlib.Path, key: str) -> typing.Any:
    pass
@typing.overload
def dicom_get( #type: ignore
    path: pathlib.Path,
    key: str, default: T) -> typing.Any | T:
    pass
def dicom_get(path, key, default=...):
    """Retrieve header element from DICOM image."""

    if default == Ellipsis:
        return pydicom.dcmread(path).get(key)
    return pydicom.dcmread(path).get(key, default)


def dicom_isdicom_file(path: pathlib.Path) -> bool:
    """
    Given path is a file, exists, and is a DICOM
    file.
    """

    if not path.exists():
        return False
    if path.is_dir():
        return False

    return magic.from_file(str(path)) == "DICOM medical imaging data"


def dicom_isroi_type(path: pathlib.Path, roi_type: str | ROIType) -> bool:
    """
    Path is to valid DICOM file and DICOM headers
    indicate the file is a valid ROI file.
    """

    if not dicom_isdicom_file(path):
        raise ValueError(f"{path!r} is not a valid DICOM image file.")

    roi_type = ROIType[roi_type] if isinstance(roi_type, str) else roi_type
    return (
        dicom_isdicom_file(path) and
        dicom_get(path, roi_type.header) == roi_type.value and #type: ignore[union-attr]
        dicom_get(path, "Modality") in roi_type.modal) #type: ignore[union-attr]


def dicom_set(
        path: pathlib.Path,
        key: str | tuple[int, int],
        VR: str,
        value: typing.Any) -> None:
    """Set the field value of a DICOM header."""

    if not dicom_isdicom_file(path):
        raise ValueError(f"{path!r} is not a valid DICOM image file.")

    dicom = pydicom.dcmread(path)
    dicom[key] = pydicom.DataElement(key, VR, value)
    pydicom.dcmwrite(path, dicom)


def ohif_error(*values: str, sep: str | None = None) -> None:
    """Write a message to `stderr`."""

    message = f"{click.style('error', fg='red')}: " + (sep or " ").join(values)
    click.echo(message, err=True)


def ohif_info(
        namespace: OHIFNamespace,
        *values: str,
        sep: str | None = None,
        level: int | None = None) -> None:
    """
    Write a message to `stdout`. Outputs only if
    `namespace.verbose` is greater than or equal
    to `level`.
    """

    if namespace.verbose < (level or 0):
        return None
    message = f"{click.style('info', fg='green')}: " + (sep or " ").join(values)
    click.echo(message)


def ohif_panic(
        *values: str,
        sep: str | None = None,
        code: int | None = None) -> typing.NoReturn:
    """Write message to `stderr` and quit."""

    ohif_error(*values, sep=sep)
    quit(code or 1)


def ohif_strict_quitter(
        code: int | None = None,
        *,
        strict: bool | None = None) -> None | typing.NoReturn:
    """
    Exits the program using the built-in `quit`
    call if called with `strict` as `True`.
    """

    if (strict := strict if strict is not None else True):
        quit(code)
    return None


def rest_auth(namespace: OHIFNamespace) -> httpx.Auth:
    """Parse credentials for the remote XNAT."""

    username, password = auth_netrc(namespace)
    # Allow for user defined override at CLI
    # execution.
    username = namespace.username or username
    password = namespace.password or password
    return httpx.BasicAuth(username, password)


@contextlib.contextmanager
def rest_client(
        namespace: OHIFNamespace,
        *,
        strict: typing.Optional[bool] = None,
        verify: typing.Optional[bool] = None):
    """
    Create a REST client to make calls against the
    remote XNAT. HTTP exceptions raised in this
    context will panic, writing a message to
    stderr.
    """

    client = httpx.Client(
        auth=rest_auth(namespace),
        base_url=rest_host(namespace),
        verify=verify if verify is not None else True)

    try:
        yield client
    except httpx.HTTPStatusError as error:
        method, path, code, phrase = rest_error_extractor(error)
        ohif_error(f"({method}) {path} failed: <{code} {phrase!r}>")
        if error.response.text:
            click.echo(error.response.text, err=True)
        ohif_strict_quitter(1, strict=strict)
    except httpx.RequestError as error:
        method, path, *_ = rest_error_extractor(error)
        ohif_error(f"({method}) {path} failed: {error}")
        ohif_strict_quitter(1, strict=strict)


def rest_host(namespace: OHIFNamespace) -> str:
    """Parse host base URL."""

    # Weed out the port number.
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
    label: typing.Optional[str],
    roi_type: str,
    overwrite: bool):
    """
    Attempt to send a PUT request to XNAT to
    store an ROI collection.
    """

    oinfo = functools.partial(ohif_info, namespace)
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

    headers = dict()
    headers["Content-Type"] = "application/octet-stream"

    params = dict()
    params["overwrite"] = str(overwrite or False).lower()
    params["seriesuid"] = ""
    params["type"]      = roi_type

    with contextlib.ExitStack() as es:
        rest  = es.enter_context(rest_client(namespace, strict=False))
        twd   = pathlib.Path(es.enter_context(tempfile.TemporaryDirectory()))

        oinfo(f"found {len(namespace.files)} DICOM files.", level=1)
        oinfo(f"pushing to store {roi_type} data.")
        for file in namespace.files:
            if not dicom_isroi_type(file, roi_type):
                oinfo(f"{file!s} modality is not of type {roi_type}.", level=3)
                oinfo(f"skipping.", level=3)
                continue

            oinfo(f"Attempting to push {file!s}", level=4)
            shutil.copyfile(str(file), str(twd.joinpath("image.dcm")))
            file = twd.joinpath("image.dcm")

            dgetter = functools.partial(dicom_get, file)
            dsetter = functools.partial(dicom_set, file)

            # Validate fields are not missing or
            # unset, and if not, set to "Unknown".
            field = dgetter("SoftwareVersions", "")
            if not field:
                oinfo(f"Fixing SoftwareVersions with field {field!r}", level=3)
                dsetter("SoftwareVersions", "LO", "Unknown")

            field = dgetter("StudyID", None)
            if field in ("", None):
                oinfo(f"Fixing StudyID with field {field!r}", level=3)
                dsetter("StudyID", "SH", "Unknown")

            # Parse the label as either a user
            # given input, or as the series
            # description from DICOM file.
            rlabel = label or dgetter("SeriesDescription").replace(" ", "_") #type: ignore[attr-defined]

            # Assign parameters based on headers
            # found in DICOM.
            rparams = params.copy()
            rparams["seriesuid"] = dicom_get(file, "SeriesInstanceUID")

            r = rest.put(
                uri_template.format(label=rlabel),
                data=es.enter_context(file.open("rb")),
                headers=headers,
                params=rparams)
            r.raise_for_status()
            oinfo(f"(PUT) {r.url.path} ({r.status_code})", level=2)

        oinfo("done.", level=2)


def rest_error_extractor(err: httpx.HTTPError) -> tuple[str, str, int, str]:
    """
    Extract error information from an
    `httpx.HTTPError`.
    """

    args = [
        err.request.method,
        err.request.url.path,
        500,
        "Internal Server Error"]

    if isinstance(err, httpx.HTTPStatusError):
        status_code = err.response.status_code
        args[2] = status_code
        args[3] = http.HTTPStatus(status_code).phrase

    return tuple(args) #type: ignore[return-value]


def rest_username(namespace: OHIFNamespace) -> str:
    """Get the REST username."""

    with rest_client(namespace) as rest:
        r = rest.get("/xapi/users/username")
        r.raise_for_status()

    return r.text


@click.group()
@click.pass_context
@click.version_option(".".join(map(str, __version__)))
@click.option("--host", "-h")
@click.option("--username", "-u", default=None)
@click.option("--password", "-p", default=None)
@click.option("--port", "-P", type=int, default=None)
@click.option("--verbose", "-v", count=True)
def ohif(
    ctx: click.Context,
    *,
    host: str,
    username: typing.Optional[str],
    password: typing.Optional[str],
    port: typing.Optional[int],
    verbose: int):
    """Manage OHIF via XNAT."""

    ctx.obj = OHIFNamespace(host, (), username, password, port, verbose)
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
@click.option(
    "--overwrite/--create",
    "-O/",
    "overwrite",
    help="create or overwrite collection")
@click.option("--file", "-f", "files", type=pathlib.Path, multiple=True)
@click.option("--label", "-l", default=None)
@click.option(
    "--type",
    "-t",
    "roi_type",
    type=click.Choice(["AIM", "RTSTRUCT", "SEG"]),
    default="SEG")
def store(
    namespace: OHIFNamespace,
    project: str,
    session: str,
    *,
    files: typing.Sequence[pathlib.Path],
    label: typing.Optional[str],
    roi_type: str,
    overwrite: bool):
    """Store an ROI collection."""

    files = dicom_find_files(*files)
    if not files:
        ohif_panic("no files were provided")

    namespace.files = files
    rest_ohif_roi_store(
        namespace,
        project,
        session,
        label=label,
        roi_type=roi_type,
        overwrite=overwrite)


def from_command_line():
    return ohif()


if __name__ == "__main__":
    exit(from_command_line())
