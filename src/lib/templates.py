import typing as t
from string import Template

from .errors import CheckerMetadataError
from .version import LooseVersion


def version_parts(version: str) -> dict[str, str]:
    """Parse version string and return a dict of named components."""
    version_list = LooseVersion.split_components(version)
    tmpl_vars = {"version": version}
    for i, version_part in enumerate(version_list):
        tmpl_vars[f"version{i}"] = version_part
        if i == 0:
            tmpl_vars["major"] = version_part
        elif i == 1:
            tmpl_vars["minor"] = version_part
        elif i == 2:
            tmpl_vars["patch"] = version_part
    return tmpl_vars


def substitute_template(template_string: str, variables: dict[str, t.Any]) -> str:
    tmpl = Template(template_string)
    try:
        return tmpl.substitute(**variables)
    except (KeyError, ValueError) as err:
        raise CheckerMetadataError("Error substituting template") from err
