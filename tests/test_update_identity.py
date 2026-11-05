import json

from src.lib.externaldata import ExternalBase
from src.lib.updateidentity import build_candidates, release_key
from src.manifest import ManifestChecker

from .update_support import UpdateFixture


class TestUpdateIdentity(UpdateFixture):
    def test_ids_survive_source_reordering_and_module_rename(self):
        checker = self.checker()
        before = {c.data.ident: c.key for c in self.candidates(checker)}
        checker = self.checker([self.source("b"), self.source("a")])
        for data in checker.get_external_data():
            data.module.name = "renamed"
        self.assertEqual(
            before, {c.data.ident: c.key for c in self.candidates(checker)}
        )

    def test_architecture_scope_is_not_default_checker_arch(self):
        checker = self.checker([self.source("a")])
        data = checker.get_external_data()[0]
        key = self.candidates(checker)[0].key
        self.assertEqual(data.arches, ["x86_64"])
        data.source["only-arches"] = ["x86_64"]
        self.assertNotEqual(self.candidates(checker)[0].key, key)
        data.source["only-arches"] = ["x86_64", "aarch64"]
        first = self.candidates(checker)[0].key
        data.source["only-arches"] = ["aarch64", "x86_64", "aarch64"]
        self.assertEqual(self.candidates(checker)[0].key, first)
        data.module.module["skip-arches"] = ["aarch64"]
        self.assertNotEqual(self.candidates(checker)[0].key, first)

    def test_shared_source_unrestricted_owner_is_part_of_architecture_scope(self):
        shared = self.root / "shared.json"
        shared.write_text(json.dumps(self.source("shared")))
        modules = [
            {"name": "all", "sources": ["shared.json"]},
            {"name": "arm", "only-arches": ["aarch64"], "sources": ["shared.json"]},
        ]

        def identity():
            self.path.write_text(
                json.dumps({"id": "org.example.App", "modules": modules})
            )
            checker = ManifestChecker(str(self.path))
            data = checker.get_external_data()[0]
            data.new_version = data.current_version._replace(version="2")
            return self.candidates(checker)[0].key

        unrestricted = identity()
        modules.pop(0)
        self.assertNotEqual(identity(), unrestricted)

    def test_inferred_identity_ambiguity_includes_unchanged_sources(self):
        sources = [self.source("a"), self.source("b")]
        for s in sources:
            del s["x-checker-data"]["source-id"]
        checker = self.checker(sources)
        checker.get_external_data()[1].new_version = None
        with self.assertLogs("src.lib.updateidentity", "WARNING"):
            self.assertEqual(self.candidates(checker), [])

    def test_github_asset_identity_survives_download_and_constraint_changes(self):
        source = self.source("a")
        source["x-checker-data"] = {
            "type": "github",
            "repo": "owner/app",
            "asset-name": "app-$version.tar.gz",
        }
        checker = self.checker([source])
        before = self.candidates(checker)[0].key
        data = checker.get_external_data()[0]
        data.source["url"] = "https://cdn.example/old.tar.gz"
        data.new_version = data.new_version._replace(
            url="https://cdn.example/new.tar.gz"
        )
        data.checker_data["tag-pattern"] = "v(.*)"
        self.assertEqual(self.candidates(checker)[0].key, before)
        data.checker_data["asset-name"] = "other-$version.tar.gz"
        self.assertNotEqual(self.candidates(checker)[0].key, before)

    def test_invalid_unchanged_checker_does_not_break_other_identities(self):
        checker = self.checker()
        invalid = checker.get_external_data()[1]
        invalid.new_version = None
        invalid.checker_data = {"type": "github"}
        self.assertEqual([c.data.ident for c in self.candidates(checker)], ["a"])

    def test_external_source_move_with_id_and_shared_yaml_alias(self):
        external = self.root / "source.json"
        external.write_text(json.dumps(self.source("a")))
        checker = self.checker(["source.json"])
        before = self.candidates(checker)[0].key
        external.rename(self.root / "moved.json")
        checker = self.checker(["moved.json"])
        self.assertEqual(self.candidates(checker)[0].key, before)
        yaml_path = self.root / "shared.yml"
        yaml_path.write_text("""id: org.example.App
modules:
  - name: first
    sources:
      - &shared
        type: archive
        url: https://example.com/app.tar.gz
        sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        x-checker-data:
          type: rotating-url
          url: https://example.com/latest
          source-id: shared
  - name: second
    only-arches: [aarch64]
    sources: [*shared]
""")
        checker = ManifestChecker(str(yaml_path))
        self.assertEqual(len(checker.get_external_data()), 1)
        data = checker.get_external_data()[0]
        data.new_version = data.current_version._replace(version="2")
        candidate = self.candidates(checker)[0]
        self.assertIn("aarch64", str(candidate.entry["source"]["arches"]))

    def test_git_tags_and_moving_refs(self):
        data = ExternalBase.from_source(
            str(self.path),
            {
                "type": "git",
                "url": "https://github.com/Owner/repo.git",
                "tag": "v1",
                "x-checker-data": {"type": "git", "source-id": "git"},
            },
        )
        data.new_version = data.current_version._replace(
            tag="v2", version="2", commit="a" * 40
        )

        def candidate():
            return build_candidates([data], self.root, {}, [])[0]

        first = candidate().key
        data.new_version = data.new_version._replace(
            commit="b" * 40, url="git@github.com:owner/repo.git"
        )
        self.assertEqual(candidate().key, first)
        data.new_version = data.new_version._replace(tag="release-2")
        self.assertNotEqual(candidate().key, first)
        data.new_version = data.new_version._replace(
            tag=None, version=None, branch="main"
        )
        first = candidate().key
        data.new_version = data.new_version._replace(commit="c" * 40)
        self.assertNotEqual(candidate().key, first)
        data.new_version = data.new_version._replace(commit="abc123")
        self.assertIsNone(release_key(data))
