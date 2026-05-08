# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""ReflectionEngine for the Termin runtime.

Provides runtime introspection of the AppSpec IR.
"""

import json


class ReflectionEngine:
    def __init__(self, app_spec_json: str):
        self._spec = json.loads(app_spec_json) if isinstance(app_spec_json, str) else app_spec_json
        self._channel_metrics = {}  # channel_name -> {sent: 0, errors: 0, lastActive: None}
        self._state_history = {}    # primitive_name -> [{from, to, at, by}]

    def content_schemas(self):
        return [t['name']['display'] for t in self._spec.get('content', [])]

    def content_count(self, name, db):
        for t in self._spec.get('content', []):
            if t['name']['display'] == name or t['name']['snake'] == name:
                return t['name']['snake']
        return None

    def content_schema(self, name):
        for t in self._spec.get('content', []):
            if t['name']['display'] == name or t['name']['snake'] == name:
                fields = [c['display_name'] for c in t['fields']]
                field_details = {}
                for c in t['fields']:
                    constraints = []
                    if c.get('required'):
                        constraints.append('required')
                    if c.get('unique'):
                        constraints.append('unique')
                    field_details[c['display_name']] = {
                        'type': c['column_type'],
                        'constraints': constraints,
                    }
                return {'fields': fields, 'field_details': field_details}
        return None

    def compute_functions(self):
        return [c['name']['display'] for c in self._spec.get('computes', [])]

    def compute_function(self, name):
        for c in self._spec.get('computes', []):
            if c['name']['display'] == name or c['name']['snake'] == name:
                return {
                    'shape': c['shape'],
                    'inputs': c.get('input_params', []),
                    'outputs': c.get('output_params', []),
                }
        return None

    def channel_state(self, name):
        return self._channel_metrics.get(name, {}).get('state', 'open')

    def channel_metrics(self, name):
        return self._channel_metrics.get(name, {'sent': 0, 'errors': 0, 'lastActive': None})

    def update_channel_metric(self, name, metric, value):
        if name not in self._channel_metrics:
            self._channel_metrics[name] = {'sent': 0, 'errors': 0, 'lastActive': None, 'state': 'open'}
        self._channel_metrics[name][metric] = value

    def identity_context(self, user):
        # isAnonymous is derived structurally from the typed Principal
        # when present — string-comparing role names is fragile because
        # v0.9 canonicalized to "Anonymous" (capital A) but historical
        # callers used "anonymous" (lowercase). The fallback path uses
        # a case-insensitive comparison so legacy callers without a
        # Principal in the user dict still get the right answer.
        principal = user.get('Principal') if isinstance(user, dict) else None
        if principal is not None:
            is_anonymous = principal.is_anonymous
        else:
            role = user.get('role', 'Anonymous') if isinstance(user, dict) else 'Anonymous'
            is_anonymous = str(role).lower() == 'anonymous'
        return {
            'role': user.get('role', 'Anonymous') if isinstance(user, dict) else 'Anonymous',
            'scopes': user.get('scopes', []) if isinstance(user, dict) else [],
            'isAnonymous': is_anonymous,
        }

    def roles(self):
        """Return list of role names."""
        return [r["name"] for r in self._spec.get("auth", {}).get("roles", [])]

    def role(self, name):
        """Return role details by name (case-insensitive)."""
        for r in self._spec.get("auth", {}).get("roles", []):
            if r["name"].lower() == name.lower():
                return {"Name": r["name"], "Scopes": r.get("scopes", [])}
        return None

    def boundary_info(self, name):
        for b in self._spec.get('boundaries', []):
            if b['name']['display'] == name or b['name']['snake'] == name:
                return b
        return None

    def boundaries(self):
        return [b['name']['display'] for b in self._spec.get('boundaries', [])]

    def channels(self):
        return [c['name']['display'] for c in self._spec.get('channels', [])]


def register_reflection_with_expr_eval(reflection: ReflectionEngine, expr_eval):
    """Register reflection accessors with the expression evaluator."""
    expr_eval.register_function('Content', type('Content', (), {
        'reflect': type('ContentReflect', (), {
            'schemas': property(lambda self: reflection.content_schemas()),
            'count': staticmethod(lambda name: reflection.content_count(name, None)),
            'schema': staticmethod(lambda name: reflection.content_schema(name)),
        })(),
    })())
    expr_eval.register_function('Compute', type('Compute', (), {
        'reflect': type('ComputeReflect', (), {
            'functions': property(lambda self: reflection.compute_functions()),
            'function': staticmethod(lambda name: reflection.compute_function(name)),
        })(),
    })())
    expr_eval.register_function('Channel', type('Channel', (), {
        'reflect': type('ChannelReflect', (), {
            'channels': property(lambda self: reflection.channels()),
            'channel': staticmethod(lambda name: reflection.channel_metrics(name)),
        })(),
    })())
    expr_eval.register_function('Boundary', type('Boundary', (), {
        'reflect': type('BoundaryReflect', (), {
            'boundaries': property(lambda self: reflection.boundaries()),
            'boundary': staticmethod(lambda name: reflection.boundary_info(name)),
        })(),
    })())
    expr_eval.register_function('Identity', type('Identity', (), {
        'reflect': type('IdentityReflect', (), {
            'role': '',
            'scopes': [],
            'isAnonymous': True,
            'hasScope': staticmethod(lambda scope: False),
        })(),
    })())

    # Role reflection: reflect_role("engineer") -> {"Name": "...", "Scopes": [...]}
    from celpy.celtypes import StringType
    def _reflect_role(name):
        role = reflection.role(str(name))
        if role:
            from celpy import json_to_cel
            return json_to_cel(role)
        return json_to_cel({"Name": str(name), "Scopes": []})
    expr_eval.register_function('reflect_role', _reflect_role)

    def _reflect_roles():
        from celpy import json_to_cel
        return json_to_cel(reflection.roles())
    expr_eval.register_function('reflect_roles', _reflect_roles)
