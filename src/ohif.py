__version__ = (0, 1, 9)

import contextlib
import dataclasses
import enum
import functools
import http
import netrc
import os
import pathlib
import shutil
import tempfile
import typing
import urllib.parse
import zipfile

import bs4
import click
import httpx
import magic
import pydicom
import pydicom.tag
import pydicom.uid

T = typing.TypeVar("T")
P = typing.ParamSpec("P")


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


class ROIType(pydicom.uid.UID, enum.ReprEnum):
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
        obj = pydicom.uid.UID.__new__(cls, value)
        obj._value_  = value
        obj._header_ = header
        obj._modal_  = modal

        return obj

    Segmentation_Storage = pydicom.uid.SegmentationStorage, "SEG"
    SEG = Segmentation_Storage
    RTStructure_Set_Storage = pydicom.uid.RTStructureSetStorage, "RTSTRUCT"
    RTSTRUCT = RTStructure_Set_Storage


class TemporaryDirectory(tempfile.TemporaryDirectory):

    def __enter__(self) -> pathlib.Path:
        return pathlib.Path(super().__enter__())


@dataclasses.dataclass
class XNATExperiment:
    ID:                   str
    id:                   str
    label:                str
    project:              str
    scanner_manufacturer: str
    scanner_model:        str
    subject_ID:           str

    date:                 str = ""
    dcmPatientBirthDate:  str = ""
    dcmPatientId:         str = ""
    dcmPatientName:       str = ""
    modality:             str = ""
    prearchivePath:       str = ""
    session_type:         str = ""
    UID:                  str = ""


@dataclasses.dataclass
class XNATScan:
    ID:                     str
    image_session_ID:       str
    project:                str
    xnat_imagescandata_id:  int
    xnat_imageScanData_id:  int

    frames:                 int = 0
    modality:               str = ""
    parameters_fov_x:       int = 0
    parameters_fov_y:       int = 0
    parameters_orientation: str = ""
    parameters_voxelRes_x:  float = 0.0
    parameters_voxelRes_y:  float = 0.0
    parameters_voxelRes_z:  float = 0.0
    quality:                str = ""
    series_class:           str = ""
    series_description:     str = ""
    type:                   str = ""
    UID:                    str = ""


@dataclasses.dataclass
class XNATSubject:
    ID:      str
    label:   str
    project: str


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

    try:
        return (netrc.netrc().authenticators(host) or default)[::2]
    except FileNotFoundError:
        return ("", "")


def dicom_find_files(
        *paths: pathlib.Path,
        strict: bool | None = None) -> typing.Sequence[pathlib.Path]:
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

        for dpath, _, dpath_files in os.walk(path):
            dpath = pathlib.Path(dpath) #type: ignore[assignment]
            if not dpath_files:
                continue
            files.extend(dicom_find_files(
                *map(dpath.joinpath, dpath_files), #type: ignore[attr-defined]
                strict=False))

    # Validate that the files found all have the
    # same StudyInstanceUID value.
    study_instance_uids = {dicom_get(f, "StudyInstanceUID") for f in files}
    if len(study_instance_uids) > 1:
        ohif_error("Files found have more than one StudyInstanceUID")
        ohif_panic(f"Found in files {study_instance_uids}")

    return tuple(files)


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


def dicom_get_xsi(path: pathlib.Path, subtype: str) -> str:
    """Get the `xsiType` from a DICOM file."""

    return f"xnat:{dicom_get(path, 'Modality').lower()}{subtype}"


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
    Path is to a DICOM file with headers that
    match the specified ROI type.
    """

    if not isinstance(roi_type, ROIType):
        roi_type = ROIType[roi_type]
    return (
        dicom_get(path, roi_type.header) == roi_type.value and
        dicom_get(path, "Modality") == roi_type.modal)


def dicom_isroi(
        path: pathlib.Path,
        roi_type: str | ROIType | None = None) -> bool:
    """
    Path is to valid DICOM file and DICOM headers
    indicate the file is a valid ROI file.
    """

    types = (roi_type,) if roi_type else ROIType.__members__.values()
    return (
        dicom_isdicom_file(path) and
        any([dicom_isroi_type(path, rt) for rt in types]))


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


def file_iszip(file: pathlib.Path):
    """
    Return if a file path points to a zip
    archive.
    """

    return (
        file.exists() and
        not file.is_dir() and
        "zip archive data" in magic.from_file(file).lower())


def ohif_error(*values: str, sep: str | None = None) -> None:
    """Write a message to `stderr`."""

    message = f"{click.style('error', fg='red')}: " + (sep or " ").join(values)
    click.echo(message, err=True)


def ohif_info(
        namespace: OHIFNamespace,
        *values: object,
        sep: str | None = None,
        level: int | None = None) -> None:
    """
    Write a message to `stdout`. Outputs only if
    `namespace.verbose` is greater than or equal
    to `level`.
    """

    if namespace.verbose < (level or 0):
        return None
    message = (
        f"{click.style('info', fg='green')}: "
        + (sep or " ").join(map(str, values)))
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
        strict: str | None = None) -> None | typing.NoReturn:
    """
    Exits the program using the built-in `quit`
    call if called with `strict` as `True`.
    """

    strict = "quitter" if strict is None else strict

    if strict == "ignore":
        return None
    elif strict == "quitter":
        quit(code)
    elif strict == "raise":
        raise
    else:
        raise ValueError(f"Unexpected strict mode type {strict!r}")


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
        strict: typing.Optional[str] = None,
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
        method, path, code, phrase = rest_extract_error(error)
        ohif_error(f"({method}) {path} failed: <{code} {phrase!r}>")
        # Extract error message from HTML.
        if error.response.text and "<html>" in error.response.text:
            html = bs4.BeautifulSoup(error.response.text, features="html.parser")
            data = html.body.find("h3").text #type: ignore[union-attr]
        else:
            data = error.response.text

        ohif_error(data)
        ohif_strict_quitter(1, strict=strict)
    except httpx.RequestError as error:
        method, path, *_ = rest_extract_error(error)
        ohif_error(f"({method}) {path} failed: {error}")
        ohif_strict_quitter(1, strict=strict)



def rest_extract_error(err: httpx.HTTPError) -> tuple[str, str, int, str]:
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


GetterFunc = typing.Callable[typing.Concatenate[OHIFNamespace, P], T]
PutterFunc = typing.Callable[typing.Concatenate[OHIFNamespace, P], typing.Any]


class REST:
    """
    Common RESTful operations against an XNAT.
    """

    @classmethod
    def _object_acquirer(
            cls,
            getter: GetterFunc[P, T],
            putter: PutterFunc[P],
            *,
            common_args: tuple | None = None,
            common_kwds: dict | None = None,
            getter_args: tuple | None = None,
            getter_kwds: dict | None = None,
            putter_args: tuple | None = None,
            putter_kwds: dict | None = None) -> T:
        """
        Attempt to get a particular object. If the
        object does not exist, try to create it
        and return the newly created object.
        """

        # Eliminate None values for vararg varkwd
        # parameters.
        common_args = common_args or ()
        common_kwds = common_kwds or {}

        args = common_args + (getter_args or ())
        kwds = common_kwds | (getter_kwds or {})
        p_getter  = functools.partial(getter, *args, **kwds)

        args = common_args + (putter_args or ())
        kwds = common_kwds | (putter_kwds or {})
        p_putter  = functools.partial(putter, *args, **kwds)

        try:
            return p_getter()
        except httpx.HTTPStatusError:
            p_putter()
            return p_getter()

    @classmethod
    def _object_getter(
        cls,
        namespace: OHIFNamespace,
        uri: str) -> dict[str, typing.Any]:
        """
        Get the raw contents of an object from an
        XNAT.
        """

        with rest_client(namespace, strict="raise") as rest:
            r = rest.get(uri, params=dict(format="json"))
            r.raise_for_status()

            ohif_info(
                namespace,
                f"(GET) {r.url.path} ({r.status_code})",
                level=4)
            return r.json()["items"][0]["data_fields"]

    @classmethod
    def _object_putter(
            cls,
            namespace: OHIFNamespace,
            uri: str,
            **params) -> str:
        """
        Raw PUT request for some XNAT endpoint.
        """

        with rest_client(namespace, strict="raise") as rest:
            r = rest.put(uri, params=params)
            r.raise_for_status()

            ohif_info(
                namespace,
                f"(PUT) {r.url.path} ({r.status_code})",
                level=4)
            return r.text

    @classmethod
    def acquire_scan(
        cls,
        namespace: OHIFNamespace,
        project: str,
        subject: str,
        session: str,
        scan: typing.Optional[str] = None,
        *,
        file: typing.Optional[pathlib.Path] = None,
        xsi_type: typing.Optional[str] = None) -> XNATScan:
        """
        Attempt to get an existing scan. If none
        exists on the remote XNAT, create the
        instance and return the created scan.
        """

        return cls._object_acquirer(
            cls.get_scan, #type: ignore[arg-type]
            cls.put_scan, #type: ignore[arg-type]
            common_args=(namespace, project, subject, session, scan),
            putter_kwds=dict(xsi_type=xsi_type, file=file)
        )

    @classmethod
    def acquire_session(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str,
            session: str,
            *,
            file: typing.Optional[pathlib.Path] = None,
            xsi_type: typing.Optional[str] = None) -> XNATExperiment:
        """
        Attempt to get an existing session. If
        none exists on the remote XNAT, create the
        instance and return the created session.
        """

        return cls._object_acquirer(
            cls.get_session, #type: ignore[arg-type]
            cls.put_session, #type: ignore[arg-type]
            common_args=(namespace, project),
            getter_args=(session,),
            putter_args=(subject, session),
            putter_kwds=dict(xsi_type=xsi_type, file=file))

    @classmethod
    def acquire_subject(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str) -> XNATSubject:
        """
        Attempt to get an existing subject. If
        none exists on the remote XNAT, create the
        instance and return the created subject.
        """

        return cls._object_acquirer(
            cls.get_subject,
            cls.put_subject,
            common_args=(namespace, project, subject))

    @classmethod
    def get_username(cls, namespace: OHIFNamespace) -> str:
        """
        Get the username associated with the REST
        session.
        """

        with rest_client(namespace, strict="quitter") as rest:
            r = rest.get("/xapi/users/username")
            r.raise_for_status()

        return r.text

    @classmethod
    def get_scan(
        cls,
        namespace: OHIFNamespace,
        project: str,
        subject: str,
        session: str,
        scan: str) -> XNATScan:
        """Get scan data from an XNAT."""

        data = cls._object_getter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}/experiments/{session}"
            f"/scans/{scan}")

        for key in data.copy().keys():
            # Must replace all '/' chars to make
            # data dataclass digestable.
            data[key.replace("/", "_")] = data.pop(key)

        return XNATScan(**data)

    @classmethod
    def get_session(
            cls,
            namespace: OHIFNamespace,
            project: str,
            session: str) -> XNATExperiment:
        """Get session data from an XNAT."""

        data = cls._object_getter(
                namespace,
                f"/data/projects/{project}/experiments/{session}")

        data["scanner_model"]        = data.pop("scanner/model", "")
        data["scanner_manufacturer"] = data.pop("scanner/manufacturer", "")
        return XNATExperiment(**data)

    @classmethod
    def get_subject(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str):
        """Get subject data from an XNAT."""

        data = cls._object_getter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}")
        return XNATSubject(**data)

    @classmethod
    def import_sessioni(
        cls,
        namespace: OHIFNamespace,
        project_id: str,
        subject_id: str,
        session_label: str,
        file: pathlib.Path,
        *,
        direct_archive: typing.Optional[bool] = None,
        handler: typing.Optional[str] = None,
        ignore_unparsable: typing.Optional[bool] = None,
        overwrite: typing.Optional[str] = None,
        overwrite_files: typing.Optional[bool] = None,
        rename: typing.Optional[bool] = None,
        quarantine: typing.Optional[bool] = None,
        trigger_pipelines: typing.Optional[bool] = None) -> None:
        """
        Attempt to send regular DICOM files to an
        XNAT as a session image.
        """

        uri = "/data/services/import"
        headers= dict()

        if file_iszip(file):
            headers["Content-Type"] = "application/zip"
        else:
            headers["Content-Type"] = "application/octet-stream"

        params = dict()
        # Only add params to import call if they
        # are explicitly declared from method
        # call. Additionally, validate if the
        # parameter is a valid param for the
        # import-handler declared.
        passthrough = lambda v: v
        bool2string = lambda v: str(v).lower()
        import_options = (
            (
                ("DICOM-zip", "gradual-DICOM"),
                "Direct-Archive",
                direct_archive,
                bool2string
            ),
            (
                ("DICOM-zip"),
                "Ignore-Unparsable",
                ignore_unparsable,
                bool2string
            ),
            (("SI",), "overwrite", overwrite, passthrough),
            (("SI",), "overwrite_files", overwrite_files, bool2string),
            (("DICOM-zip", "gradual-DICOM"), "rename", rename, bool2string),
            (("SI",), "quarantine", quarantine, bool2string),
            (("SI",), "triggerPipelines", trigger_pipelines, bool2string)
        )

        # These values are to always be set.
        params["import-handler"] = handler or "SI"
        params["inbody"]         = "true"
        params["PROJECT_ID"]     = project_id
        params["SUBJECT_ID"]     = subject_id
        params["EXPT_LABEL"]     = session_label
        for compat, name, value, factory in import_options:
            if value is None:
                continue
            if params["import-handler"] not in compat:
                raise TypeError(
                    f"{name!r} option is not compatible with "
                    f"import handler {params['import-handler']}")
            params[name] = factory(value)

        with contextlib.ExitStack() as es:
            rest = es.enter_context(rest_client(namespace, strict="raise"))
            fd   = es.enter_context(file.open("rb"))

            r = rest.post(uri, params=params, data=fd, headers=headers)
            r.raise_for_status()

            if r.status_code in range(200, 400):
                ohif_info(
                    namespace,
                    f"(POST) {r.url.path} ({r.status_code})",
                    level=4)

    @classmethod
    def put_scan(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str,
            session: str,
            scan: str,
            *,
            file: typing.Optional[pathlib.Path] = None,
            xsi_type: typing.Optional[str] = None):
        """Create a new scan on a remote XNAT."""

        if file and not xsi_type:
            xsi_type = dicom_get_xsi(file, "ScanData")
        elif not xsi_type:
            message = "Expected an xsiType or a DICOM file to extract it."
            raise ValueError(message)

        params = dict(xsiType=xsi_type)
        def add_dicom_header(param, name):
            params[f"xnat:imageScanData/{param}"] = dicom_get(file, name)

        def add_series_class():
            value = pydicom.uid.UID_dictionary[dicom_get(file, "SOPClassUID")]
            params["xnat:imageScanData/series_class"] = value[0]

        if file:
            add_dicom_header("UID", "SeriesInsanceUID")
            add_dicom_header("series_description", "SeriesDescription")
            add_dicom_header("modality", "Modality")
            add_dicom_header("type", "SeriesDescription")
            add_series_class()

        ret = cls._object_putter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}/experiments/{session}"
            f"/scans/{scan}",
            **params)

        return ret

    @classmethod
    def put_session(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: str,
            session: str,
            *,
            file: typing.Optional[pathlib.Path] = None,
            xsi_type: typing.Optional[str] = None) -> str:
        """
        Create a new session on a remote XNAT.
        """

        if file and not xsi_type:
            xsi_type = dicom_get_xsi(file, "SessionData")
        elif not xsi_type:
            message = "Expected an xsiType or a DICOM file to extract it."
            raise ValueError(message)

        params = dict(xsiType=xsi_type)
        def add_param(param, value):
            params[param] = value

        def add_image_data(param, name):
            add_param(f"xnat:imageSessionData/{param}", dicom_get(file, name))

        def add_sxn_data(param, name):
            add_param(f"xnat:experimentdata/{param}", dicom_get(file, name))

        if file:
            add_sxn_data("date", "StudyDate")
            add_image_data("modality", "Modality")

        ret = cls._object_putter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}/experiments/{session}",
            **params)

        return ret

    @classmethod
    def put_subject(
        cls,
        namespace: OHIFNamespace,
        project: str,
        subject: str) -> str:
        """
        Create a new subject on a remote XNAT.
        """

        return cls._object_putter(
            namespace,
            f"/data/projects/{project}/subjects/{subject}")


class RESTOHIF:
    """Namespace for OHIF related operations."""

    @classmethod
    def roi_store(
        cls,
        namespace: OHIFNamespace,
        project: str,
        subject: str,
        session: str,
        *,
        label: typing.Optional[str],
        roi_type: str,
        overwrite: bool) -> None:
        """
        Attempt to store segment data, and correlating
        session data, in a remote XNAT.
        """

        # Identify the xsiType from found files.
        xsi_types = set()
        for file in namespace.files:
            t = dicom_get_xsi(file, "SessionData")
            if any([n in t for n in ("aim", "seg", "rtstruct")]):
                continue
            xsi_types.add(t)

        if len(xsi_types) > 1:
            ohif_panic(f"too many xsiTypes detected ({len(xsi_types)})")
        if len(xsi_types) < 1:
            ohif_panic(f"could not determine xsiType")

        args = namespace, project, subject
        kwds = dict(xsi_type=tuple(xsi_types)[0])
        xsession = REST.acquire_session(*args, session, **kwds) #type: ignore[arg-type]
        xsubject = REST.acquire_subject(*args)

        ohif_info(
            namespace,
            f"found {len(namespace.files)} DICOM files.",
            level=1)
        ohif_info(namespace, f"attempting to upload {roi_type} data.", level=1)

        # Prepare zip file for upload of regular
        # DICOM files.
        with contextlib.ExitStack() as es:
            twd = es.enter_context(TemporaryDirectory())
            store_path = twd.joinpath("store.manifest")
            zippr_path = twd.joinpath("import.zip")

            ifd = es.enter_context(zipfile.ZipFile(zippr_path, "w"))
            sfd = es.enter_context(store_path.open("a+"))

            for file in namespace.files:
                # Add storable files to a manifest
                # to be sent to the XNAT later.
                if dicom_isroi(file, roi_type):
                    ohif_info(
                        namespace,
                        f"adding {file!s} to {store_path.name}",
                        level=2)
                    sfd.write(file.as_posix() + "\n")
                # Add regular DICOM to zip file to
                # be imported later.
                elif not dicom_isroi(file):
                    ohif_info(
                        namespace,
                        f"adding {file!s} to {zippr_path.name}",
                        level=2)
                    ifd.write(file, file.name)
                # Ignore all other files.
                else:
                    ohif_info(
                        namespace,
                        f"{file!s} not a {roi_type} file",
                        level=2)
                    ohif_info(namespace, "skipping", level=3)

            try:
                REST.import_sessioni(
                    namespace,
                    project,
                    xsubject.ID,
                    xsession.label,
                    zippr_path,
                    handler="DICOM-zip",
                    rename=True)
            except httpx.HTTPStatusError:
                pass # Cathing an error handled internally.

            # Move cursor to top of file.
            sfd.seek(0, 0)
            for filename in sfd.readlines():
                cls.roi_store_segment(
                    namespace,
                    project,
                    xsubject,
                    xsession,
                    pathlib.Path(filename.strip(os.linesep)),
                    label=label,
                    roi_type=roi_type,
                    overwrite=overwrite)

        ohif_info(namespace, "done.", level=2)

    @classmethod
    def roi_store_segment(
            cls,
            namespace: OHIFNamespace,
            project: str,
            subject: XNATSubject,
            session: XNATExperiment,
            file: pathlib.Path,
            *,
            label: typing.Optional[str],
            roi_type: str,
            overwrite: bool) -> None:
        """
        Attempt to send a PUT request to XNAT to
        store an ROI collection.
        """

        if not label:
            file_sd  = dicom_get(file, "SeriesDescription")
            file_pid = dicom_get(file, "PatientID")
            label = (
                file_sd
                .replace(" ", "_")
                .replace(file_pid, session.label)
            )

        uri = (
            f"/xapi/roi/projects/{project}"
            f"/sessions/{session.ID}/collections/{label}")

        headers = dict()
        headers["Content-Type"] = "application/octet-stream"

        params = dict()
        params["overwrite"] = str(overwrite or False).lower()
        params["type"]      = roi_type
        params["seriesuid"] = dicom_get(file, "SeriesInstanceUID")

        with contextlib.ExitStack() as es:
            rest  = es.enter_context(rest_client(namespace, strict="ignore"))
            twd = pathlib.Path(es.enter_context(tempfile.TemporaryDirectory()))

            # Create a copy of the target file to
            # validate and push to XNAT.
            shutil.copyfile(str(file), str(twd.joinpath("image.dcm")))
            file = twd.joinpath("image.dcm")
            cls.roi_validate_segment(namespace, file)

            # Push validated file to collection. 
            r = rest.put(
                uri,
                data=es.enter_context(file.open("rb")),
                headers=headers,
                params=params)
            r.raise_for_status()

            if r.status_code in range(200, 400):
                ohif_info(
                    namespace,
                    f"(PUT) {r.url.path} ({r.status_code})",
                    level=4)

    @classmethod
    def roi_validate_segment(
            cls,
            namespace: OHIFNamespace,
            file: pathlib.Path) -> None:
        """
        Validate a segment file. Ensure data is
        clean and of what the OHIF plugin
        expects.
        """

        # Validate fields are not missing or
        # unset, and if not, set to "Unknown".
        field = dicom_get(file, "SoftwareVersions", "")
        if not field:
            ohif_info(
                namespace,
                f"fixing SoftwareVersions with field {field!r}",
                level=3)
            dicom_set(file, "SoftwareVersions", "LO", "Unknown")

        field = dicom_get(file, "StudyID", None)
        if field in ("", None):
            ohif_info(
                namespace,
                f"fixing StudyID with field {field!r}",
                level=3)
            dicom_set(file, "StudyID", "SH", "0")

        ohif_info(namespace, "filepath:", file.as_posix(), level=4)
        for item in pydicom.dcmread(file):
            ohif_info(namespace, item, level=4)
        ohif_info(namespace, "end DICOM inspection", level=4)


@click.group()
@click.pass_context
@click.version_option(".".join(map(str, __version__)))
@click.option("--host", "-H")
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
    if host:
        REST.get_username(ctx.obj)

    return 0


@ohif.group()
def roi():
    """OHIF ROI Management."""


@roi.command
@pass_clinamespace
@click.argument("project")
@click.argument("subject")
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
    subject: str,
    session: str,
    *,
    files: typing.Sequence[pathlib.Path],
    label: typing.Optional[str],
    roi_type: str,
    overwrite: bool):
    """Store an ROI collection."""

    if not namespace.host:
        ohif_panic("no hostname was provided")

    files = dicom_find_files(*files)
    if not files:
        ohif_panic("no files were provided")

    namespace.files = files
    RESTOHIF.roi_store(
        namespace,
        project,
        subject,
        session,
        label=label,
        roi_type=roi_type,
        overwrite=overwrite)


def from_command_line():
    return ohif()


if __name__ == "__main__":
    exit(from_command_line())
