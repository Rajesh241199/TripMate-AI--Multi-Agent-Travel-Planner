"""Offline contract tests without importing backend's live services or database."""
import ast
from datetime import date
from pathlib import Path
import re
from typing import Any

source = Path(__file__).with_name("backend.py").read_text(encoding="utf-8")
module = ast.parse(source)
functions = {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}
needed = ['_user_constraints', '_parse_budget_estimate', 'validate_trip_draft', 'route_after_validation', 'route_after_approval']
namespace = {'re': re, 'date': date, 'Any': Any, 'MAX_REVISIONS': 2,
             'MONTHS': {name.lower(): i for i, name in enumerate(('January','February','March','April','May','June','July','August','September','October','November','December'),1)},
             'KNOWN_AIRPORTS': {
    'chennai': 'MAA', 'coimbatore': 'CJB', 'bengaluru': 'BLR', 'dubai': 'DXB'
}}
code = compile(ast.Module(body=[functions[name] for name in needed], type_ignores=[]), '<offline>', 'exec',
               flags=__import__('__future__').annotations.compiler_flag, dont_inherit=True)
exec(code, namespace)
extract = namespace['_user_constraints']
validate = namespace['validate_trip_draft']

request = "Plan a 5 days Dubai trip from Chennai with flights, hotels and sightseeing under 2 lakhs. I am planning to visit on coming December"
constraints = extract(request)
assert constraints['origin'] == 'Chennai'
assert constraints['destination'] == 'Dubai'
assert constraints['duration_days'] == 5
assert constraints['budget_limit_inr'] == 200000
assert constraints.get('travelers') is None
assert constraints['travel_month'].startswith('December ')
assert namespace['_parse_budget_estimate']('ESTIMATED_TOTAL_INR: 179,600\nESTIMATE_SCOPE: total') == (179600, 'total')

valid = '''## Chennai to Dubai travel plan
### Day 1 — Fly from Chennai MAA to Dubai DXB
### Day 2 — Explore Dubai
### Day 3 — Dubai Mall
### Day 4 — Desert safari in Dubai
### Day 5 — Return flight from Dubai DXB to Chennai MAA
Prices are estimates; December is a seasonal expectation, not a live forecast.
'''
state={'itinerary': valid, 'trip_constraints':constraints, 'budget_estimate_inr':179600, 'budget_estimate_scope':'per_person', 'weather_live':True}
errors, warnings = validate(state)
assert not errors, errors
assert any('traveler count' in w.lower() for w in warnings), warnings
assert any('budget compliance' in w.lower() for w in warnings), warnings

state['itinerary'] = valid.replace('MAA to Dubai', 'CJB to Dubai').replace('### Day 5 — Return flight from Dubai DXB to Chennai MAA', '')
errors, _ = validate(state)
assert any('CJB' in e for e in errors), errors
assert any('Missing day' in e for e in errors), errors
assert any('Return journey' in e for e in errors), errors

state['itinerary'] = valid
state['trip_constraints'] = {**constraints, 'travelers':2}
state['budget_estimate_scope']='total'
state['budget_estimate_inr']=220000
errors, _=validate(state)
assert any('exceeds maximum' in e for e in errors), errors
state['budget_estimate_inr']=179600
errors, _=validate(state)
assert not errors, errors
assert namespace['route_after_validation']({'validation_passed':True,'revision_count':0}) == 'human_approval'
assert namespace['route_after_validation']({'validation_passed':False,'revision_count':0}) == 'revision_agent'
assert namespace['route_after_validation']({'validation_passed':False,'revision_count':2}) == 'validation_failed'
assert namespace['route_after_approval']({'approved':False,'revision_count':0}) == 'revision_agent'
print('PASS: input constraints, budget extraction, conflicting flight code, duration, return trip, budget cap, validation routing')
