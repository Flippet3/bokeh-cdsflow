from collections import defaultdict
from enum import Enum, auto
import os
import re
from typing import Any, ClassVar, Literal, Sequence, cast

from bokeh.document import Document
from bokeh.embed import components
from bokeh.events import DocumentReady
from bokeh.models import ColumnDataSource, CustomJS
from bokeh.models.dom import DOMElement


JsType = Literal["number", "string", "boolean", "object", "array", "date", "bigint", "symbol", "function", "undefined", "null", "map", "set"]  # fmt: skip

class InputType(Enum):
    SingleValue = auto()
    Array = auto()

class CdsFlowCol:
    """
    Declarative column placeholder used in ``CdsFlowBase`` subclasses.

    It starts out parent-less (declared at class definition time). When a ``CdsFlow`` is
    constructed, it registers itself as the parent and assigns the final column name.
    From then on, this object can derive flow-level metadata (like ``input_type``)
    via ``self.parent``.
    """

    _parent: "CdsFlowBase | None"
    _name: str | None
    js_type: JsType
    initial_value: list[Any]

    def __init__(self, js_type: JsType, initial_value: list[Any], /):
        self._parent = None
        self._name = None
        self.js_type = js_type
        self.initial_value = initial_value
    
    @property
    def name(self) -> str:
        if self._name is None:
            raise ValueError("Tried to access name of Column before adding it to a parent CdsFlow class.")
        return self._name
    
    @property
    def parent(self) -> "CdsFlowBase":
        if self._parent is None:
            raise ValueError("Tried to access parent of Column before adding it to a parent CdsFlow class.")
        return self._parent

    @property
    def js_attr_name(self) -> str:
        if self.parent is None or not self.name:
            raise ValueError("Unlinked CdsFlowStr has no js_attr_name (missing parent/name).")
        return f"{self.parent.name}_{self.name}"

    @property
    def js_attr_type(self) -> str:
        if self.parent.input_type == InputType.SingleValue:
            return self.js_type
        return f"{self.js_type}[]"

    @property
    def js_data_accessor(self) -> str:
        if self.parent is None or not self.name:
            raise ValueError("Unlinked CdsFlowStr has no js_data_accessor (missing parent/name).")
        return f"{self.parent.name}.data.{self.name}"

    @property
    def js_input(self) -> str:
        if self.parent.input_type == InputType.SingleValue:
            return f"{self.js_data_accessor}[0]"
        return f"[... {self.js_data_accessor}]"


class CdsFlowBase:
    """
    Instance-backed CDS flow row.

    Subclasses declare columns as class attributes using ``CdsFlowCol(...)``.
    Instances build a concrete ``CdsFlow`` (and therefore ``ColumnDataSource``)
    so dependencies can be specified per instance.
    """
    input_type = InputType.Array

    def __init__(self, key: str | None = None, *, depends: Sequence["CdsFlowBase | CdsFlowCol"] = ()):
        self.key = key or ""
        cls = self.__class__
        self.base_name = re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()
        self.name = self.base_name if not self.key else f"{self.base_name}_{self.key}"

        declared: list[CdsFlowCol] = []
        # Discover declared columns on every instantiation (including inheritance).
        # Base class columns appear earlier than subclass columns.
        for base in reversed(cls.__mro__):
            if base in (object, CdsFlowBase):
                continue
            for col_name, val in base.__dict__.items():
                if col_name.startswith("_"):
                    continue
                if isinstance(val, CdsFlowCol) and val._parent is None:
                    val._name = col_name
                    declared.append(val)

        # Create per-instance column objects so `.parent` can differ per instance.
        cols: list[CdsFlowCol] = []
        col_map: dict[str, CdsFlowCol] = {}
        for decl in declared:
            c = CdsFlowCol(decl.js_type, list(decl.initial_value))
            c._name = cast(str, decl._name)
            cols.append(c)
            col_map[c._name] = c
        self._cols: dict[str, CdsFlowCol] = col_map
        self._columns: list[CdsFlowCol] = cols

        linked_deps: list[CdsFlowCol] = []
        for item in depends:
            if isinstance(item, CdsFlowBase):
                linked_deps.extend(item.columns.values())
            elif isinstance(item, CdsFlowCol):
                linked_deps.append(item)
            else:
                raise TypeError("depends must contain only CdsFlowBase instances or CdsFlowCol instances")

        for col in self._columns:
            col._parent = self
        self.source = ColumnDataSource({col.name: col.initial_value for col in self._columns if col.name is not None})
        self.depends_on_columns = linked_deps

    def __getattribute__(self, name: str) -> Any:
        # Route column handles to instance-bound objects.
        if name not in {"_cols", "source"}:
            cols = object.__getattribute__(self, "_cols") if "_cols" in object.__getattribute__(self, "__dict__") else None
            if cols is not None and name in cols:
                return cols[name]
        return object.__getattribute__(self, name)

    @property
    def columns(self) -> dict[str, CdsFlowCol]:
        if not hasattr(self, "_columns"):
            return {}
        return {col.name: col for col in self._columns if col.name is not None}

    @property
    def dependencies(self) -> set[str]:
        deps: set[str] = set()
        for col in self.depends_on_columns:
            deps.add(col.parent.name)
        return deps

    @property
    def callback_name(self) -> str:
        return f"update_{self.callback_group}"

    def callback_location(self, file_dir: str) -> str:
        return os.path.join(file_dir, f"{self.callback_group}.js")

    @property
    def callback_group(self) -> str:
        if not self.key:
            return self.base_name
        for col in self.depends_on_columns:
            if getattr(col.parent, "key", "") != self.key:
                return self.name
        return self.base_name

    def canonical_dep_param_name(self, linked_col: CdsFlowCol) -> str:
        parent = linked_col.parent
        if self.key and getattr(parent, "key", "") == self.key:
            parent_base = getattr(parent, "base_name", parent.name)
            return f"{parent_base}_{linked_col.name}"
        return linked_col.js_attr_name

    def _update_signature(self, file_dir: str) -> None:
        START_MARKER = "// === AUTOGENERATED START ==="
        END_MARKER = "// === AUTOGENERATED END ==="

        if len(self.dependencies) == 0:
            return

        js_path = self.callback_location(file_dir)
        if not os.path.exists(js_path):
            with open(js_path, "w") as f:
                f.write(f"{START_MARKER}\n")
                f.write(f"{END_MARKER}\n")
                f.write(f"  console.log('Running function {self.callback_name}');\n")
                f.write(f"""  return {{{", ".join(f"'{col}': this_{col}" for col in self.columns)}}}\n""")
                f.write("}\n")

        with open(js_path, "r+") as f:
            contents = f.read()
            if START_MARKER not in contents or END_MARKER not in contents:
                raise ValueError(f"File '{js_path}' is missing start or end marker")

            start_idx = contents.index(START_MARKER) + len(START_MARKER)
            end_idx = contents.index(END_MARKER)

            jsdoc_lines = [f" * @param {{{col.js_attr_type}}} this_{col.name}" for col in self.columns.values()]
            jsdoc_lines += [
                f" * @param {{{linked_col.js_attr_type}}} {self.canonical_dep_param_name(linked_col)}"
                for linked_col in self.depends_on_columns
            ]

            params = [f"this_{col.name}" for col in self._columns if col.name is not None] + [
                self.canonical_dep_param_name(linked_col) for linked_col in self.depends_on_columns
            ]

            if self._columns:
                returns_contents = ", ".join(f"{col.name}: {col.js_attr_type}" for col in self.columns.values())
                returns_line = f" * @returns {{{{{returns_contents}}}}}"
            else:
                returns_line = " * @returns {{}}"
            jsdoc_text = "/**\n"
            if jsdoc_lines:
                jsdoc_text += "\n".join(jsdoc_lines) + "\n"
            jsdoc_text += returns_line + "\n"
            jsdoc_text += " */\n"
            func_signature = f"function {self.callback_name}({', '.join(params)}) {{\n"

            between_markers = jsdoc_text + func_signature

            new_contents = contents[:start_idx] + "\n" + between_markers + contents[end_idx:]

            f.seek(0)
            f.write(new_contents)
            f.truncate()

    def set_value_str(self, update_dict: dict[CdsFlowCol, str]) -> str:
        str_update_dict = {key.name: value for (key, value) in update_dict.items()}
        for key, value in str_update_dict.items():
            if key not in self.columns:
                raise KeyError(f"'{key}' is not a valid column name for {self.name}")
            if not value.startswith("[") and value.endswith("]"):
                raise ValueError(f"'{key}' has value {value}. It needs to start with '[' and end with ']'.")
            

        items = []
        for key, col in self.columns.items():
            if key in str_update_dict:
                val = str_update_dict[key]
            else:
                val = f"[... {col.js_data_accessor}]"
            # Value is expected to be a string that will be placed in JS array notation
            item_str = f"{key}: {val}"
            items.append(item_str)

        data_str = f"{self.name}.data = {{{', '.join(items)}}};console.log('hi');"
        return data_str


class CdsFlowManager:
    def __init__(self, cds_flows: Sequence[CdsFlowBase], js_dir: str, tick_ms: int, engine_setup: str, engine_code: str):
        if not (isinstance(js_dir, str) and os.path.isdir(js_dir)):
            raise ValueError(f"js_dir '{js_dir}' is not a valid directory")
        self.js_dir = js_dir
        self.cds_flows = {flow.name: flow for flow in cds_flows}
        self.doc = Document()
        self.engine_code = engine_code
        self.engine_setup = engine_setup
        self.tick_ms = tick_ms

    def update_signatures(self):
        for flow in self.cds_flows.values():
            flow._update_signature(self.js_dir)

    def clear_js_files(self):
        response = input(f"Are you sure? This will delete all .js files in {self.js_dir}. Only by entering 'y' will this actually happen.").lower()
        if response != "y":
            return
        for filename in os.listdir(self.js_dir):
            if filename.endswith(".js"):
                file_path = os.path.join(self.js_dir, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)

    def get_components_and_script(self, dom_elements: dict[str, DOMElement] | None = None) -> tuple[str, dict[str, str]]:
        if dom_elements is None:
            dom_elements = {}
        self.doc = Document()
        self._attach_loop(self.doc)

        new_dom_elements = dict(dom_elements)
        for value in dom_elements.values():
            self.doc.add_root(value)
        for i, flow in enumerate(self.cds_flows.values()):
            new_dom_elements[f"source_{i}"] = flow.source
            self.doc.add_root(flow.source)

        assert set(new_dom_elements.values()) == set(self.doc.roots)

        script, divs = components(new_dom_elements)
        return script, divs

    def _attach_loop(self, doc: Document) -> None:
        self.update_signatures()
        callbacks = "\n"
        for flow in self.cds_flows.values():
            if len(flow.dependencies) > 0:
                with open(flow.callback_location(self.js_dir), "r") as f:
                    cb = f.read()
                callbacks += cb + "\n"

        dirty_requests: defaultdict[str, list[str]] = defaultdict(list)
        for flow in self.cds_flows.values():
            for dependency in flow.dependencies:
                dirty_requests[dependency].append(flow.name)

        unresolved = sorted(self.cds_flows.keys())
        resolved: set[str] = set()
        graph: list[str] = []
        while len(unresolved) > 0:
            for unresolved_name in unresolved:
                unresolved_flow = self.cds_flows[unresolved_name]
                if len(unresolved_flow.dependencies - resolved) == 0:
                    graph.append(unresolved_name)
                    resolved.add(unresolved_name)
                    unresolved.remove(unresolved_name)
                    break
            else:
                raise ValueError(f"Couldn't establish non-circular dependency graph. Resolved: {resolved}. Graph so far: {graph}")

        update_script = ""
        for cds_name in graph:
            flow = self.cds_flows[cds_name]
            reflog_checks = [
                f'refLog["{flow.name}"]["{col.name}"] != {flow.name}.data.{col.name}' for col in flow.columns.values()
            ]
            update_script += f"""
                // check if need for update
                if (!dirty.includes("{flow.name}")) {{
                    if ({" || ".join(reflog_checks)}) {{
                        dirty.push("{flow.name}");
                    }}
                }}
                if (dirty.includes("{flow.name}")) {{"""
            if len(flow.dependencies) > 0:
                args = [col.js_input for col in list(flow.columns.values()) + flow.depends_on_columns]
                if flow.input_type == InputType.SingleValue:
                    assignments = [f"'{col.name}': [new_data.{col.name}]" for col in flow.columns.values()]
                else:
                    assignments = [f"'{col.name}': new_data.{col.name}" for col in flow.columns.values()]

                update_script += f"""
                    new_data = {flow.callback_name}({", ".join(args)});
                    {flow.name}.data = {{
                        {", ".join(assignments)}
                    }};"""
            if len(dirty_requests[flow.name]) > 0:
                update_script += f"""
                    ["{'","'.join(dirty_requests[flow.name])}"].forEach((dep) => {{if (!dirty.includes(dep) && dep !== "") {{dirty.push(dep)}}}});"""
            update_script += f"""
                    dirty.splice(dirty.indexOf('{flow.name}'), 1);"""

            update_script += f"""
                    {"; ".join(it.replace("!=", "=") for it in reflog_checks)};

                }}
            """

        c = CustomJS(
            args={flow.name: flow.source for flow in self.cds_flows.values()},
            code=f"""
                {callbacks}

                var refLog = {{{",".join(f"'{graph_it}': {{}}" for graph_it in graph)}}};
                var dirty = [];
                var new_data;
                {self.engine_setup}

                function run_update_script() {{
                    {self.engine_code}
                    {update_script}
                }};
                setInterval(run_update_script, {self.tick_ms});
            """,
        )
        doc.js_on_event(DocumentReady, c)