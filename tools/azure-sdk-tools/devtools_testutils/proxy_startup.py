# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------

import os
import logging
import shlex
import time
import signal
import platform
import shutil
import tarfile
from typing import Optional
import zipfile

import certifi
from dotenv import load_dotenv, find_dotenv
import pytest
import subprocess
from urllib3.exceptions import SSLError

from ci_tools.variables import in_ci

from .config import PROXY_URL
from .fake_credentials import FAKE_ACCESS_TOKEN, FAKE_ID, SERVICEBUS_FAKE_SAS, SANITIZED
from .helpers import get_http_client, is_live_and_not_recording
from .sanitizers import (
    add_batch_sanitizers,
    Sanitizer,
    set_custom_default_matcher,
)


load_dotenv(find_dotenv())

# Raise urllib3's exposed logging level so that we don't see tons of warnings while polling the proxy's availability
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

_LOGGER = logging.getLogger()

CONTAINER_STARTUP_TIMEOUT = 60
PROXY_MANUALLY_STARTED = os.getenv("PROXY_MANUAL_START", False)

PROXY_CHECK_URL = PROXY_URL + "/Info/Available"
TOOL_ENV_VAR = "PROXY_PID"

AVAILABLE_TEST_PROXY_BINARIES = {
    "Windows": {
        "AMD64": {
            "system": "Windows",
            "machine": "AMD64",
            "file_name": "test-proxy-standalone-win-x64.zip",
            "executable": "Azure.Sdk.Tools.TestProxy.exe",
        },
    },
    "Linux": {
        "X86_64": {
            "system": "Linux",
            "machine": "X86_64",
            "file_name": "test-proxy-standalone-linux-x64.tar.gz",
            "executable": "Azure.Sdk.Tools.TestProxy",
        },
        "ARM64": {
            "system": "Linux",
            "machine": "ARM64",
            "file_name": "test-proxy-standalone-linux-arm64.tar.gz",
            "executable": "Azure.Sdk.Tools.TestProxy",
        },
    },
    "Darwin": {
        "X86_64": {
            "system": "Darwin",
            "machine": "X86_64",
            "file_name": "test-proxy-standalone-osx-x64.zip",
            "executable": "Azure.Sdk.Tools.TestProxy",
        },
        "ARM64": {
            "system": "Darwin",
            "machine": "ARM64",
            "file_name": "test-proxy-standalone-osx-arm64.zip",
            "executable": "Azure.Sdk.Tools.TestProxy",
        },
    },
}

PROXY_DOWNLOAD_URL = "https://github.com/Azure/azure-sdk-tools/releases/download/Azure.Sdk.Tools.TestProxy_{}/{}"

discovered_roots = []


def get_target_version(repo_root: str) -> str:
    """Gets the target test-proxy version from the target_version.txt file in /eng/common/testproxy"""
    version_file_location = os.path.relpath("eng/common/testproxy/target_version.txt")
    version_file_location_from_root = os.path.abspath(os.path.join(repo_root, version_file_location))

    with open(version_file_location_from_root, "r") as f:
        target_version = f.read().strip()

    return target_version


def get_downloaded_version(repo_root: str) -> Optional[str]:
    """Gets version from downloaded_version.txt within the local download folder"""

    downloaded_version_file = os.path.abspath(os.path.join(repo_root, ".proxy", "downloaded_version.txt"))

    if os.path.exists(downloaded_version_file):
        with open(downloaded_version_file, "r") as f:
            version = f.read().strip()
            return version
    else:
        return None


def ascend_to_root(start_dir_or_file: str) -> str:
    """Given a path, ascend until encountering a folder with a `.git` folder present within it. Return that directory.

    :param str start_dir_or_file: The starting directory or file. Either is acceptable.
    """
    if os.path.isfile(start_dir_or_file):
        current_dir = os.path.dirname(start_dir_or_file)
    else:
        current_dir = start_dir_or_file

    while current_dir is not None and not (os.path.dirname(current_dir) == current_dir):
        possible_root = os.path.join(current_dir, ".git")

        # we need the git check to prevent ascending out of the repo
        if os.path.exists(possible_root):
            if current_dir not in discovered_roots:
                discovered_roots.append(current_dir)
            return current_dir
        else:
            current_dir = os.path.dirname(current_dir)

    raise Exception(f'Requested target "{start_dir_or_file}" does not exist within a git repo.')


def check_availability() -> None:
    """Attempts request to /Info/Available. If a test-proxy instance is responding, we should get a response."""
    try:
        http_client = get_http_client(raise_on_status=False)
        response = http_client.request(method="GET", url=PROXY_CHECK_URL, timeout=10)
        return response.status
    # We get an SSLError if the container is started but the endpoint isn't available yet
    except SSLError as sslError:
        _LOGGER.debug(sslError)
        return 404
    except Exception as e:
        _LOGGER.debug(e)
        return 404


def check_certificate_location(repo_root: str) -> None:
    """Checks for a certificate bundle containing the test proxy's self-signed certificate.

    If a certificate bundle either isn't present or doesn't contain the correct test proxy certificate, a bundle is
    automatically created. SSL_CERT_DIR and REQUESTS_CA_BUNDLE are set to point to this bundle for the session.
    """

    existing_root_pem = certifi.where()
    local_dev_cert = os.path.abspath(os.path.join(repo_root, 'eng', 'common', 'testproxy', 'dotnet-devcert.crt'))
    combined_filename = os.path.basename(local_dev_cert).split(".")[0] + ".pem"
    combined_folder = os.path.join(repo_root, '.certificate')
    combined_location = os.path.join(combined_folder, combined_filename)

    # If no local certificate folder exists, create one
    if not os.path.exists(combined_folder):
        _LOGGER.info("Missing a test proxy certificate under azure-sdk-for-python/.certificate. Creating one now.")
        os.mkdir(combined_folder)

    def write_dev_cert_bundle():
        """Creates a certificate bundle with the test proxy certificate, followed by the user's existing CA bundle."""
        _LOGGER.info("Writing latest test proxy certificate to local certificate bundle.")
        # Copy the dev cert's content into the new certificate bundle
        with open(local_dev_cert, "r") as f:
            data = f.read()
        with open(combined_location, "w") as f:
            f.write(data)

        # Copy the existing CA bundle contents into the repository's certificate bundle
        with open(existing_root_pem, "r") as f:
            content = f.readlines()
        with open(combined_location, "a") as f:
            f.writelines(content)

    # If the certificate bundle isn't set up, set it up. If the bundle is present, make sure that it starts with the
    # correct certificate from eng/common/testproxy/dotnet-devcert.crt (to account for certificate rotation)
    if not os.path.exists(combined_location):
        write_dev_cert_bundle()
    else:
        with open(local_dev_cert, "r") as f:
            repo_cert = f.read()
        with open(combined_location, "r") as f:
            # The bundle should start with the test proxy's cert; only read as far in as the cert's length
            bundle_data = f.read(len(repo_cert))
        if repo_cert != bundle_data:
            write_dev_cert_bundle()

    _LOGGER.info(
        "Setting SSL_CERT_DIR, SSL_CERT_FILE, and REQUESTS_CA_BUNDLE environment variables for the current session.\n"
        f"SSL_CERT_DIR={combined_folder}\n"
        f"SSL_CERT_FILE=REQUESTS_CA_BUNDLE={combined_location}"
    )
    os.environ["SSL_CERT_DIR"] = combined_folder
    os.environ["SSL_CERT_FILE"] = combined_location
    os.environ["REQUESTS_CA_BUNDLE"] = combined_location


def check_proxy_availability() -> None:
    """Waits for the availability of the test-proxy."""
    start = time.time()
    now = time.time()
    status_code = 0
    while now - start < CONTAINER_STARTUP_TIMEOUT and status_code != 200:
        status_code = check_availability()
        now = time.time()


def prepare_local_tool(repo_root: str) -> str:
    """Returns the path to a downloaded executable."""

    target_proxy_version = get_target_version(repo_root)

    download_folder = os.path.join(repo_root, ".proxy")

    system = platform.system()  # Darwin, Linux, Windows
    machine = platform.machine().upper()  # arm64, x86_64, AMD64

    if system in AVAILABLE_TEST_PROXY_BINARIES:
        available_for_system = AVAILABLE_TEST_PROXY_BINARIES[system]

        if machine in available_for_system:
            target_info = available_for_system[machine]

            downloaded_version = get_downloaded_version(repo_root)
            download_necessary = not downloaded_version == target_proxy_version

            if download_necessary:
                if os.path.exists(download_folder):
                    # cleanup the directory for re-download
                    shutil.rmtree(download_folder)
                os.makedirs(download_folder)

                download_url = PROXY_DOWNLOAD_URL.format(target_proxy_version, target_info["file_name"])
                download_file = os.path.join(download_folder, target_info["file_name"])

                http_client = get_http_client()
                with open(download_file, "wb") as out:
                    r = http_client.request("GET", download_url, preload_content=False)
                    shutil.copyfileobj(r, out)

                if download_file.endswith(".zip"):
                    with zipfile.ZipFile(download_file, "r") as zip_ref:
                        zip_ref.extractall(download_folder)

                if download_file.endswith(".tar.gz"):
                    with tarfile.open(download_file) as tar_ref:
                        tar_ref.extractall(download_folder)

                os.remove(download_file)  # Remove downloaded file after contents are extracted

                # Record downloaded version for later comparison with target version in repo
                with open(os.path.join(download_folder, "downloaded_version.txt"), "w") as f:
                    f.writelines([target_proxy_version])

            executable_path = os.path.join(download_folder, target_info["executable"])
            # Mark the executable file as executable by all users; Mac drops these permissions during extraction
            if system == "Darwin":
                os.chmod(executable_path, 0o755)
            return os.path.abspath(executable_path).replace("\\", "/")
        else:
            _LOGGER.error(f'There are no available standalone proxy binaries for platform "{machine}".')
            raise Exception(
                "Unable to download a compatible standalone proxy for the current platform. File an issue against "
                "Azure/azure-sdk-tools with this error."
            )
    else:
        _LOGGER.error(f'There are no available standalone proxy binaries for system "{system}".')
        raise Exception(
            "Unable to download a compatible standalone proxy for the current system. File an issue against "
            "Azure/azure-sdk-tools with this error."
        )


def set_common_sanitizers() -> None:
    """Register sanitizers that will apply to all recordings throughout the SDK."""
    batch_sanitizers = {}

    # Remove headers from recordings if we don't need them, and ignore them if present
    # Authorization, for example, can contain sensitive info and can cause matching failures during challenge auth
    headers_to_ignore = "Authorization, x-ms-client-request-id, x-ms-request-id"
    set_custom_default_matcher(excluded_headers=headers_to_ignore)
    batch_sanitizers[Sanitizer.REMOVE_HEADER] = [{"headers": headers_to_ignore}]

    # Remove OAuth interactions, which can contain client secrets and aren't necessary for playback testing
    batch_sanitizers[Sanitizer.OAUTH_RESPONSE] = [None]

    # Body key sanitizers for sensitive fields in JSON requests/responses
    batch_sanitizers[Sanitizer.BODY_KEY] = [
        {"json_path": "$..access_token", "value": FAKE_ACCESS_TOKEN},
        {"json_path": "$..AccessToken", "value": FAKE_ACCESS_TOKEN},
        {"json_path": "$..targetModelLocation", "value": SANITIZED},
        {"json_path": "$..targetResourceId", "value": SANITIZED},
        {"json_path": "$..urlSource", "value": SANITIZED},
        {"json_path": "$..azureBlobSource.containerUrl", "value": SANITIZED},
        {"json_path": "$..source", "value": SANITIZED},
        {"json_path": "$..resourceLocation", "value": SANITIZED},
        {"json_path": "Location", "value": SANITIZED},
        {"json_path": "$..to", "value": SANITIZED},
        {"json_path": "$..from", "value": SANITIZED},
        {"json_path": "$..sasUri", "value": SANITIZED},
        {"json_path": "$..containerUri", "value": SANITIZED},
        {"json_path": "$..inputDataUri", "value": SANITIZED},
        {"json_path": "$..outputDataUri", "value": SANITIZED},
        # {"json_path": "$..id", "value": SANITIZED},
        {"json_path": "$..token", "value": SANITIZED},
        {"json_path": "$..appId", "value": SANITIZED},
        {"json_path": "$..userId", "value": SANITIZED},
        {"json_path": "$..storageAccount", "value": SANITIZED},
        {"json_path": "$..resourceGroup", "value": SANITIZED},
        {"json_path": "$..guardian", "value": SANITIZED},
        {"json_path": "$..scan", "value": SANITIZED},
        {"json_path": "$..catalog", "value": SANITIZED},
        {"json_path": "$..lastModifiedBy", "value": SANITIZED},
        {"json_path": "$..managedResourceGroupName", "value": SANITIZED},
        {"json_path": "$..friendlyName", "value": SANITIZED},
        {"json_path": "$..createdBy", "value": SANITIZED},
        {"json_path": "$..credential", "value": SANITIZED},
        {"json_path": "$..aliasPrimaryConnectionString", "value": SANITIZED},
        {"json_path": "$..aliasSecondaryConnectionString", "value": SANITIZED},
        {"json_path": "$..connectionString", "value": SANITIZED},
        {"json_path": "$..primaryConnectionString", "value": SANITIZED},
        {"json_path": "$..secondaryConnectionString", "value": SANITIZED},
        {"json_path": "$..sshPassword", "value": SANITIZED},
        {"json_path": "$..primaryKey", "value": SANITIZED},
        {"json_path": "$..secondaryKey", "value": SANITIZED},
        {"json_path": "$..runAsPassword", "value": SANITIZED},
        {"json_path": "$..adminPassword", "value": SANITIZED},
        {"json_path": "$..adminPassword.value", "value": SANITIZED},
        {"json_path": "$..administratorLoginPassword", "value": SANITIZED},
        {"json_path": "$..accessSAS", "value": SANITIZED},
        {"json_path": "$..WEBSITE_AUTH_ENCRYPTION_KEY", "value": SANITIZED},
        {"json_path": "$..storageContainerWriteSas", "value": SANITIZED},
        {"json_path": "$..storageContainerUri", "value": SANITIZED},
        {"json_path": "$..storageContainerReadListSas", "value": SANITIZED},
        {"json_path": "$..storageAccountPrimaryKey", "value": SANITIZED},
        {"json_path": "$..uploadUrl", "value": SANITIZED},
        {"json_path": "$..secondaryReadonlyMasterKey", "value": SANITIZED},
        {"json_path": "$..primaryMasterKey", "value": SANITIZED},
        {"json_path": "$..primaryReadonlyMasterKey", "value": SANITIZED},
        {"json_path": "$..secondaryMasterKey", "value": SANITIZED},
        {"json_path": "$..scriptUrlSasToken", "value": SANITIZED},
        {"json_path": "$..privateKey", "value": SANITIZED},
        {"json_path": "$..password", "value": SANITIZED},
        {"json_path": "$..logLink", "value": SANITIZED},
        {"json_path": "$..keyVaultClientSecret", "value": SANITIZED},
        {"json_path": "$..httpHeader", "value": SANITIZED},
        {"json_path": "$..functionKey", "value": SANITIZED},
        {"json_path": "$..fencingClientPassword", "value": SANITIZED},
        {"json_path": "$..encryptedCredential", "value": SANITIZED},
        {"json_path": "$..clientSecret", "value": SANITIZED},
        {"json_path": "$..certificatePassword", "value": SANITIZED},
        {"json_path": "$..authHeader", "value": SANITIZED},
        {"json_path": "$..atlasKafkaSecondaryEndpoint", "value": SANITIZED},
        {"json_path": "$..atlasKafkaPrimaryEndpoint", "value": SANITIZED},
        {"json_path": "$..appkey", "value": SANITIZED},
        {"json_path": "$..acrToken", "value": SANITIZED},
        {"json_path": "$..accountKey", "value": SANITIZED},
        {"json_path": "$..accountName", "value": SANITIZED},
        {"json_path": "$..decryptionKey", "value": SANITIZED},
        {"json_path": "$..applicationId", "value": SANITIZED},
        {"json_path": "$..apiKey", "value": SANITIZED},
        {"json_path": "$..userName", "value": SANITIZED},
        {"json_path": "$.properties.DOCKER_REGISTRY_SERVER_PASSWORD", "value": SANITIZED},
        {"json_path": "$.value[*].key", "value": SANITIZED},
        # {"json_path": "$.key", "value": SANITIZED},
        {"json_path": "$..clientId", "value": FAKE_ID},
        {"json_path": "$..principalId", "value": FAKE_ID},
        {"json_path": "$..tenantId", "value": FAKE_ID},
    ]

    # Body regex sanitizers for sensitive patterns in request/response bodies
    batch_sanitizers[Sanitizer.BODY_REGEX] = [
        {"regex": "(client_id=)[^&]+", "value": "$1sanitized"},
        {"regex": "(client_secret=)[^&]+", "value": "$1sanitized"},
        {"regex": "(client_assertion=)[^&]+", "value": "$1sanitized"},
        {"regex": "(?:[\\?&](sv|sig|se|srt|ss|sp)=)(?<secret>(([^&\\s]*)))", "value": SANITIZED},
        {"regex": "refresh_token=(?<group>.*?)(?=&|$)", "group_for_replace": "group", "value": SANITIZED},
        {"regex": "access_token=(?<group>.*?)(?=&|$)", "group_for_replace": "group", "value": SANITIZED},
        {"regex": "token=(?<token>[^\\u0026]+)($|\\u0026)", "group_for_replace": "token", "value": SANITIZED},
        {"regex": "-----BEGIN PRIVATE KEY-----\\n(.+\\n)*-----END PRIVATE KEY-----\\n", "value": SANITIZED},
        {"regex": "(?<=<UserDelegationKey>).*?(?:<SignedTid>)(.*)(?:</SignedTid>)", "value": SANITIZED},
        {"regex": "(?<=<UserDelegationKey>).*?(?:<SignedOid>)(.*)(?:</SignedOid>)", "value": SANITIZED},
        {"regex": "(?<=<UserDelegationKey>).*?(?:<Value>)(.*)(?:</Value>)", "value": SANITIZED},
        {"regex": "(?:Password=)(.*?)(?:;)", "value": SANITIZED},
        {"regex": "(?:User ID=)(.*?)(?:;)", "value": SANITIZED},
        {"regex": "(?:<PrimaryKey>)(.*)(?:</PrimaryKey>)", "value": SANITIZED},
        {"regex": "(?:<SecondaryKey>)(.*)(?:</SecondaryKey>)", "value": SANITIZED},
    ]

    # General regex sanitizers for sensitive patterns throughout interactions
    batch_sanitizers[Sanitizer.GENERAL_REGEX] = [
        {"regex": "SharedAccessKey=(?<key>[^;\\\"]+)", "group_for_replace": "key", "value": SANITIZED},
        {"regex": "AccountKey=(?<key>[^;\\\"]+)", "group_for_replace": "key", "value": SANITIZED},
        {"regex": "accesskey=(?<key>[^;\\\"]+)", "group_for_replace": "key", "value": SANITIZED},
        {"regex": "Accesskey=(?<key>[^;\\\"]+)", "group_for_replace": "key", "value": SANITIZED},
        {"regex": "Secret=(?<key>[^;\\\"]+)", "group_for_replace": "key", "value": SANITIZED},
    ]

    # Header regex sanitizers for sensitive patterns in request/response headers
    batch_sanitizers[Sanitizer.HEADER_REGEX] = [
        {"key": "subscription-key", "value": SANITIZED},
        {"key": "x-ms-encryption-key", "value": SANITIZED},
        {"key": "x-ms-rename-source", "value": SANITIZED},
        {"key": "x-ms-file-rename-source", "value": SANITIZED},
        {"key": "x-ms-copy-source", "value": SANITIZED},
        {"key": "x-ms-copy-source-authorization", "value": SANITIZED},
        {"key": "x-ms-file-rename-source-authorization", "value": SANITIZED},
        {"key": "x-ms-encryption-key-sha256", "value": SANITIZED},
        {"key": "api-key", "value": SANITIZED},
        {"key": "aeg-sas-token", "value": SANITIZED},
        {"key": "aeg-sas-key", "value": SANITIZED},
        {"key": "aeg-channel-name", "value": SANITIZED},
        {"key": "SupplementaryAuthorization", "value": SERVICEBUS_FAKE_SAS},
    ]

    # URI regex sanitizers for sensitive patterns in request/response URLs
    batch_sanitizers[Sanitizer.URI_REGEX] = [
        {"regex": "sig=(?<sig>[^&]+)", "group_for_replace": "sig", "value": SANITIZED}
    ]

    # Send all the above sanitizers to the test proxy in a single, batch request
    add_batch_sanitizers(sanitizers=batch_sanitizers)


def start_test_proxy(request) -> None:
    """Starts the test proxy and returns when the proxy server is ready to receive requests.

    In regular use cases, this will auto-start the test-proxy docker container. In CI, or when environment variable
    TF_BUILD is set, this function will start the test-proxy .NET tool.
    """

    repo_root = ascend_to_root(request.node.items[0].module.__file__)
    check_certificate_location(repo_root)

    if not PROXY_MANUALLY_STARTED:
        if check_availability() == 200:
            _LOGGER.debug("Tool is responding, exiting...")
        else:
            root = os.getenv("BUILD_SOURCESDIRECTORY", repo_root)
            _LOGGER.info("{} is calculated repo root".format(root))

            # If we're in CI, allow for tox environment parallelization and write proxy output to a log file
            log = None
            if in_ci():
                envname = os.getenv("TOX_ENV_NAME", "default")
                log = open(os.path.join(root, "_proxy_log_{}.log".format(envname)), "a")

                os.environ["PROXY_ASSETS_FOLDER"] = os.path.join(root, "l", envname)
                if not os.path.exists(os.environ["PROXY_ASSETS_FOLDER"]):
                    os.makedirs(os.environ["PROXY_ASSETS_FOLDER"])

            if os.getenv("TF_BUILD"):
                _LOGGER.info("Starting the test proxy tool from dotnet tool cache...")
                tool_name = "test-proxy"
            else:
                _LOGGER.info("Downloading and starting standalone proxy executable...")
                tool_name = prepare_local_tool(root)

            # Always start the proxy with these two defaults set to allow SSL connection
            passenv = {
                "ASPNETCORE_Kestrel__Certificates__Default__Path": os.path.join(
                    root, "eng", "common", "testproxy", "dotnet-devcert.pfx"
                ),
                "ASPNETCORE_Kestrel__Certificates__Default__Password": "password",
            }
            # If they are already set, override what we give the proxy with what is in os.environ
            passenv.update(os.environ)

            proc = subprocess.Popen(
                shlex.split(f'{tool_name} start --storage-location="{root}" -- --urls "{PROXY_URL}"'),
                stdout=log or subprocess.DEVNULL,
                stderr=log or subprocess.STDOUT,
                env=passenv,
            )
            os.environ[TOOL_ENV_VAR] = str(proc.pid)

    # Wait for the proxy server to become available
    check_proxy_availability()
    set_common_sanitizers()


def stop_test_proxy() -> None:
    """Stops any running instance of the test proxy"""

    if not PROXY_MANUALLY_STARTED:
        _LOGGER.info("Stopping the test proxy tool...")

        try:
            os.kill(int(os.getenv(TOOL_ENV_VAR)), signal.SIGTERM)
        except:
            _LOGGER.debug("Unable to kill running test-proxy process.")


@pytest.fixture(scope="session")
def test_proxy(request) -> None:
    """Pytest fixture to be used before running any tests that are recorded with the test proxy"""
    if is_live_and_not_recording():
        yield
    else:
        start_test_proxy(request)
        # Everything before this yield will be run before fixtures that invoke this one are run
        # Everything after it will be run after invoking fixtures are done executing
        yield
        stop_test_proxy()
