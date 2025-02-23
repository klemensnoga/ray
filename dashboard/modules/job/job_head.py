import aiohttp.web
from aiohttp.web import Request, Response
import dataclasses
from functools import wraps
import logging
from typing import Any, Callable
import json
import traceback
from dataclasses import dataclass

import ray
import ray.dashboard.utils as dashboard_utils
from ray._private.runtime_env.packaging import (package_exists,
                                                upload_package_to_gcs)
from ray.dashboard.modules.job.common import (
    http_uri_components_to_uri,
    JobStatus,
    JobSubmitRequest,
    JobSubmitResponse,
    JobStopResponse,
    JobStatusResponse,
    JobLogsResponse,
    validate_request_type,
)
from ray.dashboard.modules.job.job_manager import JobManager

logger = logging.getLogger(__name__)
routes = dashboard_utils.ClassMethodRouteTable

RAY_INTERNAL_JOBS_NAMESPACE = "_ray_internal_jobs"


def _init_ray_and_catch_exceptions(f: Callable) -> Callable:
    @wraps(f)
    async def check(self, *args, **kwargs):
        if not ray.is_initialized():
            ray.init(address="auto", namespace=RAY_INTERNAL_JOBS_NAMESPACE)
        try:
            return await f(self, *args, **kwargs)
        except Exception as e:
            logger.exception(f"Unexpected error in handler: {e}")
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code)

    return check


class JobHead(dashboard_utils.DashboardHeadModule):
    def __init__(self, dashboard_head):
        super().__init__(dashboard_head)

        self._job_manager = None

    async def _parse_and_validate_request(self, req: Request,
                                          request_type: dataclass) -> Any:
        """Parse request and cast to request type. If parsing failed, return a
        Response object with status 400 and stacktrace instead.
        """
        try:
            return validate_request_type(await req.json(), request_type)
        except Exception as e:
            logger.info(f"Got invalid request type: {e}")
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPBadRequest.status_code)

    def job_exists(self, job_id: str) -> bool:
        status = self._job_manager.get_job_status(job_id)
        return status is not None

    @routes.get("/api/packages/{protocol}/{package_name}")
    @_init_ray_and_catch_exceptions
    async def get_package(self, req: Request) -> Response:
        package_uri = http_uri_components_to_uri(
            protocol=req.match_info["protocol"],
            package_name=req.match_info["package_name"])

        if not package_exists(package_uri):
            return Response(
                text=f"Package {package_uri} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code)

        return Response()

    @routes.put("/api/packages/{protocol}/{package_name}")
    @_init_ray_and_catch_exceptions
    async def upload_package(self, req: Request):
        package_uri = http_uri_components_to_uri(
            protocol=req.match_info["protocol"],
            package_name=req.match_info["package_name"])
        logger.info(f"Uploading package {package_uri} to the GCS.")
        try:
            upload_package_to_gcs(package_uri, await req.read())
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code)

        return Response(status=aiohttp.web.HTTPOk.status_code)

    @routes.post("/api/jobs/")
    @_init_ray_and_catch_exceptions
    async def submit_job(self, req: Request) -> Response:
        result = await self._parse_and_validate_request(req, JobSubmitRequest)
        # Request parsing failed, returned with Response object.
        if isinstance(result, Response):
            return result
        else:
            submit_request = result

        try:
            job_id = self._job_manager.submit_job(
                entrypoint=submit_request.entrypoint,
                job_id=submit_request.job_id,
                runtime_env=submit_request.runtime_env,
                metadata=submit_request.metadata)

            resp = JobSubmitResponse(job_id=job_id)
        except (TypeError, ValueError):
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPBadRequest.status_code)
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code)

        return Response(
            text=json.dumps(dataclasses.asdict(resp)),
            content_type="application/json",
            status=aiohttp.web.HTTPOk.status_code,
        )

    @routes.post("/api/jobs/{job_id}/stop")
    @_init_ray_and_catch_exceptions
    async def stop_job(self, req: Request) -> Response:
        job_id = req.match_info["job_id"]
        if not self.job_exists(job_id):
            return Response(
                text=f"Job {job_id} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code)

        try:
            stopped = self._job_manager.stop_job(job_id)
            resp = JobStopResponse(stopped=stopped)
        except Exception:
            return Response(
                text=traceback.format_exc(),
                status=aiohttp.web.HTTPInternalServerError.status_code)

        return Response(
            text=json.dumps(dataclasses.asdict(resp)),
            content_type="application/json")

    @routes.get("/api/jobs/{job_id}")
    @_init_ray_and_catch_exceptions
    async def get_job_status(self, req: Request) -> Response:
        job_id = req.match_info["job_id"]
        if not self.job_exists(job_id):
            return Response(
                text=f"Job {job_id} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code)

        status: JobStatus = self._job_manager.get_job_status(job_id)
        resp = JobStatusResponse(status=status)
        return Response(
            text=json.dumps(dataclasses.asdict(resp)),
            content_type="application/json")

    @routes.get("/api/jobs/{job_id}/logs")
    @_init_ray_and_catch_exceptions
    async def get_job_logs(self, req: Request) -> Response:
        job_id = req.match_info["job_id"]
        if not self.job_exists(job_id):
            return Response(
                text=f"Job {job_id} does not exist",
                status=aiohttp.web.HTTPNotFound.status_code)

        logs: str = self._job_manager.get_job_logs(job_id)
        # TODO(jiaodong): Support log streaming #19415
        resp = JobLogsResponse(logs=logs)
        return Response(
            text=json.dumps(dataclasses.asdict(resp)),
            content_type="application/json")

    async def run(self, server):
        if not self._job_manager:
            self._job_manager = JobManager()
