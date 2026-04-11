"""Deploy/build identifier for once-per-build management commands (ECS, CodeBuild, etc.)."""
import os


def get_deploy_build_id():
    """
    Return a string that changes each deploy/image build.

    Set one of these in the task definition / CI so commands can run at most once per deploy
    (e.g. clear_public_caches, enhance_permissions) while scale-out skips duplicates.
    """
    return (
        os.environ.get("BUILD_ID")
        or os.environ.get("IMAGE_TAG")
        or os.environ.get("GIT_SHA")
        or os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION")
    )
