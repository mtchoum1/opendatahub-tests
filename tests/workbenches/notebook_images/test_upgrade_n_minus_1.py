"""N-1 workbench image survival tests across RHOAI platform upgrades."""

import pytest
from ocp_resources.notebook import Notebook
from ocp_resources.pod import Pod

from tests.workbenches.notebook_images.utils import (
    UPGRADE_MARKER_CONTENT,
    StatefulSet,
    WorkbenchImageBaseline,
    WorkbenchImageSpec,
    get_workbench_image_specs,
    grab_and_check_pod_logs,
    read_pvc_upgrade_marker,
    verify_notebook_generation_unchanged,
    verify_notebook_image_digest_unchanged,
    verify_notebook_image_selection_unchanged,
    verify_notebook_pod_not_recreated,
    verify_notebook_restart_counts_unchanged,
    verify_statefulset_healthy,
    wait_for_http_inside_pod,
)
from utilities.constants import Timeout

_WORKBENCH_IMAGE_SPECS = get_workbench_image_specs()
pytestmark = [
    pytest.mark.tier2,
    pytest.mark.slow,
    pytest.mark.parametrize(
        argnames="workbench_image_spec",
        argvalues=_WORKBENCH_IMAGE_SPECS,
        ids=[spec.ide for spec in _WORKBENCH_IMAGE_SPECS],
        indirect=True,
    ),
]


class TestPreUpgradeNMinusOneWorkbench:
    """Launch workbenches on N-1 images before the platform upgrade."""

    @pytest.mark.pre_upgrade
    def test_workbench_pre_upgrade_health(
        self,
        n_minus_one_notebook: Notebook,
        n_minus_one_pod: Pod,
        n_minus_one_baseline: WorkbenchImageBaseline,
        workbench_image_spec: WorkbenchImageSpec,
    ) -> None:
        """Given a workbench pinned to the source ImageStream tag,
        When pre-upgrade validation runs,
        Then the pod is Ready, logs are clean, HTTP responds when applicable, and the baseline is captured.
        """
        assert n_minus_one_notebook.exists
        assert n_minus_one_pod.exists
        assert n_minus_one_baseline.image_tag

        container_name = workbench_image_spec.notebook_name
        grab_and_check_pod_logs(pod=n_minus_one_pod, container_name=container_name)
        if workbench_image_spec.probe_http:
            wait_for_http_inside_pod(
                pod=n_minus_one_pod,
                container_name=container_name,
                namespace=n_minus_one_notebook.namespace,
                notebook_name=n_minus_one_notebook.name,
            )
        grab_and_check_pod_logs(pod=n_minus_one_pod, container_name=container_name)


class TestPostUpgradeNMinusOneWorkbench:
    """Verify N-1 workbench images remain healthy after the platform upgrade."""

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(name="workbench_exists")
    def test_workbench_exists_after_upgrade(
        self,
        n_minus_one_notebook: Notebook,
        n_minus_one_pod: Pod,
    ) -> None:
        """Given the pre-upgrade workbench,
        When the platform upgrade completes,
        Then the Notebook CR and original pod still exist.
        """
        assert n_minus_one_notebook.exists
        assert n_minus_one_pod.exists
        n_minus_one_pod.wait_for_condition(
            condition=Pod.Condition.READY,
            status=Pod.Condition.Status.TRUE,
            timeout=Timeout.TIMEOUT_5MIN,
        )

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(name="pod_not_recreated", depends=["workbench_exists"])
    def test_pod_not_recreated_after_upgrade(
        self,
        n_minus_one_pod: Pod,
        n_minus_one_baseline: WorkbenchImageBaseline,
    ) -> None:
        """Given the pre-upgrade baseline,
        When the upgraded cluster reconciles the workbench,
        Then the original pod creation timestamp is preserved.
        """
        verify_notebook_pod_not_recreated(pod=n_minus_one_pod, baseline=n_minus_one_baseline)

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(name="image_selection_unchanged", depends=["pod_not_recreated"])
    def test_image_selection_unchanged_after_upgrade(
        self,
        n_minus_one_notebook: Notebook,
        n_minus_one_baseline: WorkbenchImageBaseline,
    ) -> None:
        """Given the pre-upgrade baseline,
        When the Notebook CR is inspected after upgrade,
        Then the last selected image annotation is unchanged.
        """
        verify_notebook_image_selection_unchanged(notebook=n_minus_one_notebook, baseline=n_minus_one_baseline)

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(name="image_digest_unchanged", depends=["image_selection_unchanged"])
    def test_image_digest_unchanged_after_upgrade(
        self,
        n_minus_one_notebook: Notebook,
        n_minus_one_pod: Pod,
        n_minus_one_baseline: WorkbenchImageBaseline,
        workbench_image_spec: WorkbenchImageSpec,
    ) -> None:
        """Given the pre-upgrade baseline,
        When the running container image is inspected after upgrade,
        Then the resolved digest matches the pre-upgrade digest.
        """
        verify_notebook_image_digest_unchanged(
            pod=n_minus_one_pod,
            container_name=workbench_image_spec.notebook_name,
            baseline=n_minus_one_baseline,
        )

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(name="restart_counts_unchanged", depends=["image_digest_unchanged"])
    def test_restart_counts_unchanged_after_upgrade(
        self,
        n_minus_one_pod: Pod,
        n_minus_one_baseline: WorkbenchImageBaseline,
    ) -> None:
        """Given the pre-upgrade baseline,
        When pod restart counts are compared after upgrade,
        Then no container restart count has increased.
        """
        verify_notebook_restart_counts_unchanged(pod=n_minus_one_pod, baseline=n_minus_one_baseline)

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(name="notebook_generation_unchanged", depends=["restart_counts_unchanged"])
    def test_notebook_generation_unchanged_after_upgrade(
        self,
        n_minus_one_notebook: Notebook,
        n_minus_one_baseline: WorkbenchImageBaseline,
    ) -> None:
        """Given the pre-upgrade baseline,
        When the Notebook CR generation is compared after upgrade,
        Then the generation is unchanged.
        """
        verify_notebook_generation_unchanged(notebook=n_minus_one_notebook, baseline=n_minus_one_baseline)

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(name="statefulset_healthy", depends=["notebook_generation_unchanged"])
    def test_statefulset_healthy_after_upgrade(
        self,
        n_minus_one_statefulset: StatefulSet,
    ) -> None:
        """Given the pre-upgrade workbench,
        When the StatefulSet is inspected after upgrade,
        Then readyReplicas matches spec.replicas and no rollout is pending.
        """
        verify_statefulset_healthy(statefulset=n_minus_one_statefulset)

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(name="pvc_data_survives", depends=["statefulset_healthy"])
    def test_pvc_data_survives_upgrade(
        self,
        n_minus_one_pod: Pod,
        n_minus_one_baseline: WorkbenchImageBaseline,
        workbench_image_spec: WorkbenchImageSpec,
    ) -> None:
        """Given a marker file was written to the PVC before upgrade,
        When the upgrade completes,
        Then the marker content is still readable from the PVC.
        """
        marker_content = read_pvc_upgrade_marker(
            pod=n_minus_one_pod,
            container_name=workbench_image_spec.notebook_name,
        )
        assert marker_content == n_minus_one_baseline.upgrade_marker == UPGRADE_MARKER_CONTENT, (
            f"PVC marker mismatch for {workbench_image_spec.ide}. "
            f"Expected '{n_minus_one_baseline.upgrade_marker}', got '{marker_content}'"
        )

    @pytest.mark.post_upgrade
    @pytest.mark.dependency(depends=["pvc_data_survives"])
    def test_workbench_health_after_upgrade(
        self,
        n_minus_one_notebook: Notebook,
        n_minus_one_pod: Pod,
        workbench_image_spec: WorkbenchImageSpec,
    ) -> None:
        """Given the surviving workbench after upgrade,
        When logs and in-pod HTTP are checked again,
        Then the health checks pass without new log errors.
        """
        container_name = workbench_image_spec.notebook_name
        grab_and_check_pod_logs(pod=n_minus_one_pod, container_name=container_name)
        if workbench_image_spec.probe_http:
            wait_for_http_inside_pod(
                pod=n_minus_one_pod,
                container_name=container_name,
                namespace=n_minus_one_notebook.namespace,
                notebook_name=n_minus_one_notebook.name,
            )
        grab_and_check_pod_logs(pod=n_minus_one_pod, container_name=container_name)
