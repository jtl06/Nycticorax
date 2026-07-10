from pathlib import Path
import unittest


class DeploymentFileTests(unittest.TestCase):
    def test_docker_build_excludes_local_secrets_and_state(self) -> None:
        dockerignore = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

        self.assertIn(".env", dockerignore)
        self.assertIn(".git", dockerignore)
        self.assertIn(".venv", dockerignore)

    def test_container_runs_as_unprivileged_user(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("useradd --create-home --uid 10001 nycti", dockerfile)
        self.assertIn("\nUSER nycti\n", dockerfile)

    def test_compose_database_is_internal_and_password_has_no_default(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?", compose)
        self.assertNotIn('"5432:5432"', compose)
        self.assertIn("no-new-privileges:true", compose)


if __name__ == "__main__":
    unittest.main()
