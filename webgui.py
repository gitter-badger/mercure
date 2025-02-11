"""
webgui.py
=========
The web-based graphical user interface of mercure.
"""

# Standard python includes
import uvicorn
import base64
import sys
import shutil
import json
import distro
import os
import datetime
import logging
import daiquiri
import html
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union, List
import docker
import hupper
import nomad
import base64

# Starlette-related includes
from starlette.applications import Starlette
from starlette.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.responses import PlainTextResponse
from starlette.responses import JSONResponse
from starlette.responses import RedirectResponse
from starlette.authentication import requires
from starlette.authentication import (
    AuthenticationBackend,
    SimpleUser,
    AuthCredentials,
)
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.config import Config
from starlette.datastructures import URL, Secret
from starlette.routing import Route, Router

# App-specific includes
import common.config as config
import common.monitor as monitor
from common.constants import mercure_defs, mercure_names

import webinterface.users as users
import webinterface.tagslist as tagslist
import webinterface.services as services
import webinterface.rules as rules
import webinterface.targets as targets
import webinterface.modules as modules
import webinterface.queue as queue
from webinterface.common import *


###################################################################################
## Helper classes
###################################################################################

daiquiri.setup(
    config.get_loglevel(),
    outputs=(
        daiquiri.output.Stream(
            formatter=daiquiri.formatter.ColorFormatter(
                fmt="%(color)s%(levelname)-8.8s " "%(name)s: %(message)s%(color_stop)s"
            )
        ),
    ),
)
logger = daiquiri.getLogger("webgui")


try:
    nomad_connection = nomad.Nomad(host="172.17.0.1", timeout=5)
    logger.info("Connected to Nomad")
except:
    nomad_connection = None


class ExtendedUser(SimpleUser):
    def __init__(self, username: str, is_admin: bool = False) -> None:
        self.username = username
        self.admin_status = is_admin

    @property
    def is_admin(self) -> bool:
        return self.admin_status


class SessionAuthBackend(AuthenticationBackend):
    async def authenticate(self, request):

        username = request.session.get("user")
        if username == None:
            return

        credentials = ["authenticated"]
        is_admin = False

        if request.session.get("is_admin", "False") == "Jawohl":
            credentials.append("admin")
            is_admin = True

        return AuthCredentials(credentials), ExtendedUser(username, is_admin)


webgui_config = Config(
    (
        os.getenv("MERCURE_CONFIG_FOLDER")
        or os.path.realpath(os.path.dirname(os.path.realpath(__file__)) + "/configuration")
    )
    + "/webgui.env"
)


# Note: PutSomethingRandomHere is the default value in the shipped configuration file.
#       The app will not start with this value, forcing the users to set their onw secret
#       key. Therefore, the value is used as default here as well.
SECRET_KEY = webgui_config("SECRET_KEY", cast=Secret, default="PutSomethingRandomHere")
WEBGUI_PORT = webgui_config("PORT", cast=int, default=8000)
WEBGUI_HOST = webgui_config("HOST", default="0.0.0.0")
DEBUG_MODE = webgui_config("DEBUG", cast=bool, default=True)


app = Starlette(debug=DEBUG_MODE)
# Don't check the existence of the static folder because the wrong parent folder is used if the
# source code is parsed by sphinx. This would raise an exception and lead to failure of sphinx.
app.mount("/static", StaticFiles(directory="webinterface/statics", check_dir=False), name="static")
app.add_middleware(AuthenticationMiddleware, backend=SessionAuthBackend())
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="mercure_session")
app.mount("/rules", rules.rules_app)
app.mount("/targets", targets.targets_app)
app.mount("/modules", modules.modules_app)
app.mount("/users", users.users_app)
app.mount("/queue", queue.queue_app)


###################################################################################
## Logs endpoints
###################################################################################


@app.route("/logs")
@requires(["authenticated", "admin"], redirect="login")
async def show_first_log(request) -> Response:
    """Get the first service entry and forward to corresponding log entry point."""
    if services.services_list:
        first_service = next(iter(services.services_list))
        return RedirectResponse(url="/logs/" + first_service, status_code=303)
    else:
        return PlainTextResponse("No services configured")


def get_nomad_logs(service) -> bytes:
    allocations = nomad_connection.job.get_allocations("mercure")
    alloc_id = next((a["ID"] for a in allocations if a["ClientStatus"] == "running"))

    def nomad_log_type(type="stderr") -> Any:
        return nomad_connection.client.stream_logs.stream(alloc_id, service, type, origin="end", offset=10000)

    log_response = nomad_log_type() or nomad_log_type("stdout")
    return base64.b64decode(json.loads(log_response).get("Data", ""))


@app.route("/logs/{service}")
@requires(["authenticated", "admin"], redirect="login")
async def show_log(request) -> Response:
    """Render the log for the given service. The time range can be specified via URL parameters."""
    requested_service = request.path_params["service"]

    # Get optional start and end dates from the URL. Make sure that the date format is clean.
    start_obj: Optional[datetime.datetime]

    try:
        start_date = request.query_params.get("from", "")
        start_time = request.query_params.get("from_time", "00:00")
        start_timestamp = f"{start_date} {start_time}"
        start_obj = datetime.datetime.strptime(start_timestamp, "%Y-%m-%d %H:%M")
    except ValueError:
        start_obj = None
        start_timestamp = ""

    try:
        end_date = request.query_params.get("to", "")
        # Make sure end time includes the day-of, unless otherwise specified
        end_time = request.query_params.get("to_time", "23:59")
        end_timestamp = f"{end_date} {end_time}"
        datetime.datetime.strptime(end_timestamp, "%Y-%m-%d %H:%M")
    except ValueError:
        end_timestamp = ""

    service_logs = {}
    for service in services.services_list:
        service_logs[service] = {
            "id": service,
            "name": services.services_list[service]["name"],
            "systemd": services.services_list[service].get("systemd_service", ""),
            "docker": services.services_list[service].get("docker_service", ""),
        }

    if requested_service not in service_logs:
        return PlainTextResponse("Service does not exist.")

    if (
        "systemd_service" not in services.services_list[requested_service]
        and "docker_service" not in services.services_list[requested_service]
    ):
        return PlainTextResponse("Service incorrectly configured.")

    return_code = -1
    raw_logs = bytes()

    # Get information about the type of mercure installation on the server
    runtime = config.get_runner()

    # Fetch the log files depending on how mercure has been installed
    if runtime == "nomad" and nomad_connection is not None:
        try:
            raw_logs = get_nomad_logs(requested_service)
            return_code = 0
        except:
            pass
    elif runtime == "systemd":
        start_date_cmd = ""
        end_date_cmd = ""
        if start_timestamp:
            start_date_cmd = f"--since {start_timestamp}"
        if end_timestamp:
            end_date_cmd = f"--until {end_timestamp}"

        run_result = await async_run(
            f"journalctl -n 1000 -u "
            f'{services.services_list[requested_service]["systemd_service"]} '
            f"{start_date_cmd} {end_date_cmd}"
        )
        return_code = run_result[0] or -1
        raw_logs = run_result[1]
    elif runtime == "docker":
        client = docker.from_env()
        try:
            container = client.containers.get(services.services_list[requested_service]["docker_service"])
            container.reload()
            raw_logs = container.logs(since=start_obj)
            return_code = 0
        except (docker.errors.NotFound, docker.errors.APIError):
            return_code = 1

    # return_code, raw_logs = (await async_run("/usr/bin/nomad alloc logs -job -stderr -f -tail mercure router"))[:2]
    if return_code == 0:
        log_content = html.escape(str(raw_logs.decode()))
        line_list = log_content.split("\n")
        if len(line_list) and (not line_list[-1]):
            del line_list[-1]
        log_content = "<br />".join(line_list)
    else:
        log_content = f"Error reading log information"
        if start_date or end_date:
            log_content = log_content + "<br /><br />Are the From/To settings valid?"

    template = "logs.html"
    context = {
        "request": request,
        "mercure_version": mercure_defs.VERSION,
        "page": "logs",
        "service_logs": service_logs,
        "log_id": requested_service,
        "log_content": log_content,
        "start_date": start_date,
        "start_time": start_time,
        "end_date": end_date,
        "end_time_available": runtime == "systemd",
        "start_time_available": runtime in ("docker", "systemd"),
    }
    context.update(get_user_information(request))
    return templates.TemplateResponse(template, context)


###################################################################################
## Configuration endpoints
###################################################################################


@app.route("/configuration")
@requires(["authenticated"], redirect="homepage")
async def configuration(request) -> Response:
    """Shows the current configuration of the mercure appliance."""
    try:
        config.read_config()
    except:
        return PlainTextResponse("Error reading configuration file.")
    template = "configuration.html"
    config_edited = int(request.query_params.get("edited", 0))
    os_info = distro.linux_distribution()
    os_string = f"{os_info[0]} Version {os_info[1]} ({os_info[2]})"
    runtime = config.get_runner()
    context = {
        "request": request,
        "mercure_version": mercure_defs.VERSION,
        "page": "configuration",
        "config": config.mercure,
        "os_string": os_string,
        "config_edited": config_edited,
        "runtime": runtime,
    }
    context.update(get_user_information(request))
    return templates.TemplateResponse(template, context)


@app.route("/configuration/edit")
@requires(["authenticated", "admin"], redirect="homepage")
async def configuration_edit(request) -> Response:
    """Shows a configuration editor"""

    # Check for existence of lock file
    cfg_file = Path(config.configuration_filename)
    cfg_lock = Path(cfg_file.parent / cfg_file.stem).with_suffix(mercure_names.LOCK)
    if cfg_lock.exists():
        return PlainTextResponse("Configuration is being updated. Try again in a minute.")

    try:
        with open(cfg_file, "r") as json_file:
            config_content = json.load(json_file)
    except:
        return PlainTextResponse("Error reading configuration file.")

    config_content = json.dumps(config_content, indent=4, sort_keys=False)

    template = "configuration_edit.html"
    context = {
        "request": request,
        "mercure_version": mercure_defs.VERSION,
        "page": "configuration",
        "config_content": config_content,
    }
    context.update(get_user_information(request))
    return templates.TemplateResponse(template, context)


@app.route("/configuration/edit", methods=["POST"])
@requires(["authenticated", "admin"], redirect="homepage")
async def configuration_edit_post(request) -> Response:
    """Updates the configuration after post from editor"""

    form = dict(await request.form())
    editor_json = form.get("editor", "{}")
    try:
        validated_json = json.loads(editor_json)
    except ValueError:
        return PlainTextResponse("Invalid JSON data transferred.")

    try:
        config.write_configfile(validated_json)
        config.read_config()
    except ValueError:
        return PlainTextResponse("Unable to write config file. Might be locked.")

    logger.info(f"Updates mercure configuration file.")
    monitor.send_webgui_event(monitor.w_events.CONFIG_EDIT, request.user.display_name, "")

    return RedirectResponse(url="/configuration?edited=1", status_code=303)


###################################################################################
## Login/logout endpoints
###################################################################################


@app.route("/login", methods=["GET"])
async def login(request) -> Response:
    """Shows the login page."""
    try:
        config.read_config()
    except:
        return PlainTextResponse("Error reading configuration file.")
    request.session.clear()
    template = "login.html"
    context = {
        "request": request,
        "mercure_version": mercure_defs.VERSION,
        "appliance_name": config.mercure.get("appliance_name", "master"),
    }
    return templates.TemplateResponse(template, context)


@app.route("/login", methods=["POST"])
async def login_post(request) -> Response:
    """Evaluate the submitted login information. Redirects to index page if login information valid, otherwise back to login.
    On the first login, the user will be directed to the settings page and asked to change the password."""
    try:
        users.read_users()
    except:
        return PlainTextResponse("Configuration is being updated. Try again in a minute.")

    form = dict(await request.form())

    if users.evaluate_password(form.get("username", ""), form.get("password", "")):
        request.session.update({"user": form["username"]})

        if users.is_admin(form["username"]) == True:
            request.session.update({"is_admin": "Jawohl"})

        monitor.send_webgui_event(
            monitor.w_events.LOGIN,
            form["username"],
            "{admin}".format(admin="ADMIN" if users.is_admin(form["username"]) else ""),
        )

        if users.needs_change_password(form["username"]):
            return RedirectResponse(url="/settings", status_code=303)
        else:
            return RedirectResponse(url="/", status_code=303)
    else:
        if request.client.host is None:
            source_ip = "UNKOWN IP"
        else:
            source_ip = request.client.host
        monitor.send_webgui_event(monitor.w_events.LOGIN_FAIL, form["username"], source_ip)

        template = "login.html"
        context = {
            "request": request,
            "invalid_password": 1,
            "mercure_version": mercure_defs.VERSION,
            "appliance_name": config.mercure.get("appliance_name", "mercure Router"),
        }
        return templates.TemplateResponse(template, context)


@app.route("/logout")
async def logout(request):
    """Logouts the users by clearing the session cookie."""
    monitor.send_webgui_event(monitor.w_events.LOGOUT, request.user.display_name, "")
    request.session.clear()
    return RedirectResponse(url="/login")


@app.route("/settings", methods=["GET"])
@requires(["authenticated"], redirect="login")
async def settings_edit(request) -> Response:
    """Shows the settings for the current user. Renders the same template as the normal user edit, but with parameter own_settings=True."""
    try:
        users.read_users()
    except:
        return PlainTextResponse("Configuration is being updated. Try again in a minute.")

    own_name = request.user.display_name

    template = "users_edit.html"
    context = {
        "request": request,
        "mercure_version": mercure_defs.VERSION,
        "page": "settings",
        "edituser": own_name,
        "edituser_info": users.users_list[own_name],
        "own_settings": "True",
        "change_password": users.users_list[own_name].get("change_password", "False"),
    }
    context.update(get_user_information(request))
    return templates.TemplateResponse(template, context)


###################################################################################
## Homepage endpoints
###################################################################################


@app.route("/")
@requires("authenticated", redirect="login")
async def homepage(request) -> Response:
    """Renders the index page that shows information about the system status."""
    used_space: float = 0
    free_space: Union[int, str] = 0
    total_space: Union[int, str] = 0
    disk_total: Union[int, str] = 0
    runtime = config.get_runner()

    try:
        disk_total, disk_used, disk_free = shutil.disk_usage(config.mercure.incoming_folder)

        if disk_total == 0:
            disk_total = 1

        used_space = 100 * disk_used / disk_total
        free_space = disk_free // (2 ** 30)
        total_space = disk_total // (2 ** 30)
    except:
        used_space = -1
        free_space = "N/A"
        disk_total = "N/A"

    service_status = {}
    for service in services.services_list:
        running_status: Optional[bool] = False

        if runtime == "systemd":
            if (await async_run("systemctl is-active " + services.services_list[service]["systemd_service"]))[0] == 0:
                running_status = True

        elif runtime == "docker":
            client = docker.from_env()
            try:
                container = client.containers.get(services.services_list[service]["docker_service"])
                container.reload()
                status = container.status
                """restarting, running, paused, exited"""
                if status == "running":
                    running_status = True

            except (docker.errors.NotFound, docker.errors.APIError):
                running_status = False
        elif runtime == "nomad":
            if nomad_connection is None:
                running_status = None
            else:
                allocations = nomad_connection.job.get_allocations("mercure")
                running_alloc = [a for a in allocations if a["ClientStatus"] == "running"]
                if not running_alloc:
                    running_status = False
                else:
                    alloc = running_alloc[0]
                    if not alloc["TaskStates"].get(service):
                        running_status = False
                    else:
                        running_status = alloc["TaskStates"][service]["State"] == "running"
        service_status[service] = {
            "id": service,
            "name": services.services_list[service]["name"],
            "running": running_status,
        }

    template = "index.html"
    context = {
        "request": request,
        "mercure_version": mercure_defs.VERSION,
        "page": "homepage",
        "used_space": used_space,
        "free_space": free_space,
        "total_space": total_space,
        "service_status": service_status,
    }
    context.update(get_user_information(request))
    return templates.TemplateResponse(template, context)


@app.route("/services/control", methods=["POST"])
@requires(["authenticated", "admin"], redirect="homepage")
async def control_services(request) -> Response:
    form = dict(await request.form())
    action = ""

    if form.get("action", "") == "start":
        action = "start"
    if form.get("action", "") == "stop":
        action = "stop"
    if form.get("action", "") == "restart":
        action = "restart"
    if form.get("action", "") == "kill":
        action = "kill"

    controlservices = form.get("services", "").split(",")

    if action and len(controlservices) > 0:
        for service in controlservices:
            if not str(service) in services.services_list:
                continue

            if services.services_list[service].get("systemd_service", ""):
                command = "systemctl " + action + " " + services.services_list[service]["systemd_service"]
                logger.info(f"Executing: {command}")
                await async_run(command)

            elif services.services_list[service].get("docker_service", ""):
                client = docker.from_env()
                logger.info(f'Executing: {action} on {services.services_list[service]["docker_service"]}')
                try:
                    container = client.containers.get(services.services_list[service]["docker_service"])
                    container.reload()
                    if action == "start":
                        container.start()
                    if action == "stop":
                        container.stop()
                    if action == "restart":
                        container.restart()
                    if action == "kill":
                        container.kill()

                except (docker.errors.NotFound, docker.errors.APIError) as docker_error:
                    logger.error(f"{docker_error}")
                    pass

    monitor_string = "action: " + action + "; services: " + form.get("services", "")
    monitor.send_webgui_event(monitor.w_events.SERVICE_CONTROL, request.user.display_name, monitor_string)
    return JSONResponse("{ }")


###################################################################################
## Error handlers
###################################################################################


@app.route("/error")
async def error(request):
    """
    An example error. Switch the `debug` setting to see either tracebacks or 500 pages.
    """
    raise RuntimeError("Oh no")


@app.exception_handler(404)
async def not_found(request, exc) -> Response:
    """
    Return an HTTP 404 page.
    """
    template = "404.html"
    context = {"request": request, "mercure_version": mercure_defs.VERSION}
    return templates.TemplateResponse(template, context, status_code=404)


@app.exception_handler(500)
async def server_error(request, exc) -> Response:
    """
    Return an HTTP 500 page.
    """
    template = "500.html"
    context = {"request": request, "mercure_version": mercure_defs.VERSION}
    return templates.TemplateResponse(template, context, status_code=500)


###################################################################################
## Emergency error handler
###################################################################################


async def emergency_response(request) -> Response:
    """Shows emergency message about invalid configuration."""
    return PlainTextResponse("ERROR: mercure configuration is invalid. Check configuration and restart webgui service.")


def launch_emergency_app() -> None:
    """Launches a minimal application to inform the user about the incorrect configuration"""
    # emergency_app = Starlette(debug=True)
    emergency_app = Router([Route("/{whatever:path}", endpoint=emergency_response, methods=["GET", "POST"]),])
    uvicorn.run(emergency_app, host=WEBGUI_HOST, port=WEBGUI_PORT)


###################################################################################
## Entry function
###################################################################################


def main(args=sys.argv[1:]) -> None:
    if "--reload" in args or os.getenv("MERCURE_ENV", "PROD").lower() == "dev":
        # start_reloader will only return in a monitored subprocess
        reloader = hupper.start_reloader("webgui.main")
    try:
        services.read_services()
        config.read_config()
        users.read_users()
        if str(SECRET_KEY) == "PutSomethingRandomHere":
            logger.error("You need to change the SECRET_KEY in configuration/webgui.env")
            raise Exception("Invalid or missing SECRET_KEY in webgui.env")
    except Exception as e:
        logger.error(e)
        logger.error("Cannot start service. Showing emergency message.")
        launch_emergency_app()
        logger.info("Going down.")
        sys.exit(1)

    monitor.configure("webgui", "main", config.mercure.bookkeeper)
    monitor.send_event(monitor.m_events.BOOT, monitor.severity.INFO, f"PID = {os.getpid()}")

    try:
        tagslist.read_tagslist()
    except Exception as e:
        logger.info(e)
        logger.info("Unable to parse tag list. Rule evaluation will not be available.")

    uvicorn.run(app, host=WEBGUI_HOST, port=WEBGUI_PORT)

    # Process will exit here
    monitor.send_event(monitor.m_events.SHUTDOWN, monitor.severity.INFO, "")


if __name__ == "__main__":
    main()
