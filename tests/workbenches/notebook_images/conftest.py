"""Fixtures for N-1 workbench image upgrade survival tests."""

from collections.abc import Generator
from typing import Any

import pytest
import structlog
from kubernetes.dynamic import DynamicClient
from ocp_resources.config_map import ConfigMap
from ocp_resources.namespace import Namespace
from ocp_resources.notebook import Notebook
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.pod import Pod
from ocp_resources.resource import ResourceEditor

from tests.workbenches.notebook_images.utils import (
    UPGRADE_BASELINE_CM_NAME,
    ResolvedWorkbenchImage,
    StatefulSet,
    WorkbenchImageBaseline,
    WorkbenchImageSpec,
    build_n1_notebook_dict,
    capture_workbench_baseline,
    load_workbench_baseline,
    resolve_workbench_image,
    resolve_workbench_upgrade_track,
    should_skip_workbench_spec,
    store_workbench_baseline,
    wait_for_controller_reconciliation,
    write_pvc_upgrade_marker,
)
from tests.workbenches.notebooks_server.controller.utils import notebook_service_account
from utilities.constants import Labels, Timeout
from utilities.infra import create_ns

LOGGER = structlog.get_logger(name=__name__)

UPGRADE_NAMESPACE = "upgrade-notebook-images"


@pytest.fixture(scope="session")
def workbench_image_spec(request: pytest.FixtureRequest) -> WorkbenchImageSpec:
    """Parametrized workbench IDE configuration."""
    return request.param


@pytest.fixture(scope="session")
def workbench_upgrade_track(admin_client: DynamicClient) -> str:
    """Return the configured workbench upgrade track."""
    return resolve_workbench_upgrade_track(admin_client=admin_client)


@pytest.fixture(scope="session")
def n_minus_one_namespace(
    pytestconfig: pytest.Config,
    admin_client: DynamicClient,
    unprivileged_client: DynamicClient,
    teardown_resources: bool,
) -> Generator[Namespace, Any, Any]:
    """Namespace shared by all N-1 workbench image upgrade tests."""
    namespace = Namespace(client=unprivileged_client, name=UPGRADE_NAMESPACE)

    if pytestconfig.option.post_upgrade:
        yield namespace
        namespace.client = admin_client
        if teardown_resources:
            namespace.clean_up()
    else:
        existing_ns = Namespace(client=admin_client, name=UPGRADE_NAMESPACE)
        if existing_ns.exists:
            LOGGER.info(f"Namespace {UPGRADE_NAMESPACE} already exists, reusing it")
            yield namespace
        else:
            with create_ns(
                admin_client=admin_client,
                unprivileged_client=unprivileged_client,
                name=UPGRADE_NAMESPACE,
                add_dashboard_label=True,
                teardown=teardown_resources,
            ) as created_namespace:
                yield created_namespace


@pytest.fixture(scope="session", autouse=True)
def ensure_ide_supported(
    pytestconfig: pytest.Config,
    admin_client: DynamicClient,
    workbench_image_spec: WorkbenchImageSpec,
    workbench_upgrade_track: str,
) -> None:
    """Skip unsupported IDE and cluster combinations before fixture setup."""
    skip_reason = should_skip_workbench_spec(
        admin_client=admin_client,
        spec=workbench_image_spec,
        post_upgrade=pytestconfig.option.post_upgrade,
        workbench_upgrade_track=workbench_upgrade_track,
    )
    if skip_reason:
        pytest.skip(skip_reason)


@pytest.fixture(scope="session")
def n_minus_one_baseline_configmap(
    pytestconfig: pytest.Config,
    admin_client: DynamicClient,
    n_minus_one_namespace: Namespace,
    teardown_resources: bool,
) -> Generator[ConfigMap, Any, Any]:
    """Shared ConfigMap that carries notebook baselines across the upgrade boundary."""
    config_map = ConfigMap(
        client=admin_client,
        name=UPGRADE_BASELINE_CM_NAME,
        namespace=n_minus_one_namespace.name,
        data={},
        ensure_exists=pytestconfig.option.post_upgrade,
        teardown=teardown_resources,
    )

    if pytestconfig.option.post_upgrade:
        yield config_map
    else:
        with config_map:
            yield config_map


@pytest.fixture(scope="session")
def n_minus_one_image(
    admin_client: DynamicClient,
    workbench_image_spec: WorkbenchImageSpec,
) -> ResolvedWorkbenchImage:
    """Resolved N-1 image reference for the parametrized IDE."""
    return resolve_workbench_image(admin_client=admin_client, spec=workbench_image_spec)


@pytest.fixture(scope="session")
def n_minus_one_pvc(
    pytestconfig: pytest.Config,
    unprivileged_client: DynamicClient,
    n_minus_one_namespace: Namespace,
    workbench_image_spec: WorkbenchImageSpec,
    teardown_resources: bool,
) -> Generator[PersistentVolumeClaim, Any, Any]:
    """PVC backing the N-1 workbench."""
    pvc_kwargs = {
        "client": unprivileged_client,
        "name": workbench_image_spec.pvc_name,
        "namespace": n_minus_one_namespace.name,
    }

    if pytestconfig.option.post_upgrade:
        yield PersistentVolumeClaim(**pvc_kwargs)
    else:
        existing_pvc = PersistentVolumeClaim(**pvc_kwargs)
        if existing_pvc.exists:
            LOGGER.info(f"PVC '{workbench_image_spec.pvc_name}' already exists, reusing it")
            yield existing_pvc
            return

        with PersistentVolumeClaim(
            **pvc_kwargs,
            label={Labels.OpenDataHub.DASHBOARD: "true"},
            accessmodes=PersistentVolumeClaim.AccessMode.RWO,
            size="1Gi",
            volume_mode=PersistentVolumeClaim.VolumeMode.FILE,
            teardown=teardown_resources,
        ) as pvc:
            yield pvc


@pytest.fixture(scope="session")
def n_minus_one_notebook(
    pytestconfig: pytest.Config,
    unprivileged_client: DynamicClient,
    n_minus_one_namespace: Namespace,
    n_minus_one_pvc: PersistentVolumeClaim,
    n_minus_one_image: ResolvedWorkbenchImage,
    workbench_image_spec: WorkbenchImageSpec,
    teardown_resources: bool,
) -> Generator[Notebook, Any, Any]:
    """Notebook CR launched on the N-1 workbench image."""
    notebook_kwargs = {
        "client": unprivileged_client,
        "name": workbench_image_spec.notebook_name,
        "namespace": n_minus_one_namespace.name,
    }

    if pytestconfig.option.post_upgrade:
        yield Notebook(**notebook_kwargs)
    else:
        existing_notebook = Notebook(**notebook_kwargs)
        if existing_notebook.exists:
            annotations = existing_notebook.instance.metadata.annotations or {}
            selected_image = annotations.get("notebooks.opendatahub.io/last-image-selection")
            if selected_image == n_minus_one_image.image_selection:
                LOGGER.info(f"Notebook '{workbench_image_spec.notebook_name}' already exists, reusing it")
                with notebook_service_account(
                    client=unprivileged_client,
                    name=workbench_image_spec.notebook_name,
                    namespace=n_minus_one_namespace.name,
                    teardown=False,
                ):
                    yield existing_notebook
                return

            LOGGER.warning(
                f"Notebook '{workbench_image_spec.notebook_name}' exists with image "
                f"'{selected_image}' but expected '{n_minus_one_image.image_selection}'; recreating notebook"
            )
            existing_notebook.delete()

        notebook_dict = build_n1_notebook_dict(
            namespace=n_minus_one_namespace.name,
            notebook_name=workbench_image_spec.notebook_name,
            pvc_name=workbench_image_spec.pvc_name,
            image=n_minus_one_image,
        )
        with (
            notebook_service_account(
                client=unprivileged_client,
                name=workbench_image_spec.notebook_name,
                namespace=n_minus_one_namespace.name,
                teardown=teardown_resources,
            ),
            Notebook(client=unprivileged_client, kind_dict=notebook_dict, teardown=teardown_resources) as notebook,
        ):
            yield notebook


@pytest.fixture(scope="session")
def n_minus_one_pod(
    admin_client: DynamicClient,
    unprivileged_client: DynamicClient,
    n_minus_one_notebook: Notebook,
    workbench_image_spec: WorkbenchImageSpec,
) -> Pod:
    """Notebook pod for N-1 survival tests."""
    notebook_pod = Pod(
        client=unprivileged_client,
        namespace=n_minus_one_notebook.namespace,
        name=f"{n_minus_one_notebook.name}-0",
    )
    wait_for_controller_reconciliation(
        admin_client=admin_client,
        notebook_name=workbench_image_spec.notebook_name,
        notebook_namespace=n_minus_one_notebook.namespace,
        notebook_pod=notebook_pod,
        timeout=Timeout.TIMEOUT_10MIN,
    )
    return notebook_pod


@pytest.fixture(scope="session")
def n_minus_one_statefulset(
    unprivileged_client: DynamicClient,
    n_minus_one_notebook: Notebook,
) -> StatefulSet:
    """StatefulSet owned by the Notebook CR."""
    return StatefulSet(
        client=unprivileged_client,
        name=n_minus_one_notebook.name,
        namespace=n_minus_one_notebook.namespace,
    )


@pytest.fixture(scope="session")
def n_minus_one_baseline(
    pytestconfig: pytest.Config,
    n_minus_one_baseline_configmap: ConfigMap,
    n_minus_one_notebook: Notebook,
    n_minus_one_pod: Pod,
    n_minus_one_image: ResolvedWorkbenchImage,
    workbench_image_spec: WorkbenchImageSpec,
) -> WorkbenchImageBaseline:
    """Pre/post-upgrade baseline for the parametrized workbench."""
    if pytestconfig.option.post_upgrade:
        return load_workbench_baseline(
            config_map_data=dict(n_minus_one_baseline_configmap.instance.data or {}),
            baseline_prefix=workbench_image_spec.baseline_prefix,
        )

    write_pvc_upgrade_marker(pod=n_minus_one_pod, container_name=workbench_image_spec.notebook_name)
    baseline = capture_workbench_baseline(
        notebook=n_minus_one_notebook,
        pod=n_minus_one_pod,
        resolved_image=n_minus_one_image,
    )
    updated_data = store_workbench_baseline(
        config_map_data=dict(n_minus_one_baseline_configmap.instance.data or {}),
        baseline_prefix=workbench_image_spec.baseline_prefix,
        baseline=baseline,
    )
    ResourceEditor(patches={n_minus_one_baseline_configmap: {"data": updated_data}}).update()
    LOGGER.info(f"Saved N-1 baseline for {workbench_image_spec.ide}: tag={baseline.image_tag}")
    return baseline
