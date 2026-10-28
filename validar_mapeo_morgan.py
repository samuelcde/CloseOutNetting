"""Prueba aislada de las proyecciones reales Morgan Stanley, sin acceso a PostgreSQL.
Colocar junto a historical_import.py y historical_schema_map.json.
"""
import json
from pathlib import Path
from historical_import import project_canonical

mapping = json.loads(Path('historical_schema_map.json').read_text(encoding='utf-8'))
proj = mapping['morgan_canonical_projections']

expected_forward = {
    'principal_party_name': 'counterparty_name',
    'trading_name': 'trading_name',
    'op_party_name': 'op_party_name',
}
expected_options = {
    'account_number': 'account_number',
    'taps_account_id': 'taps_account_id',
}

for source, dest in expected_forward.items():
    assert proj['FORWARD'].get(source) == dest, f'FORWARD: {source} apunta a {proj["FORWARD"].get(source)!r}, se esperaba {dest!r}'
for source, dest in expected_options.items():
    assert proj['OPTIONS'].get(source) == dest, f'OPTIONS: {source} apunta a {proj["OPTIONS"].get(source)!r}, se esperaba {dest!r}'
assert proj['OPTIONS'].get('taps_numbers') != 'taps_account_id', 'taps_numbers todavía se proyecta sobre taps_account_id'

cases = [
    ('FORWARD - Porvenir', 'FORWARD', {
        'principal_party_name': 'FONDO DE PENSIONES OBLIGATORIAS PORVENIR MODERADO',
        'trading_name': 'PORVENIR AFP',
    }, {
        'principal_party_name': 'FONDO DE PENSIONES OBLIGATORIAS PORVENIR MODERADO',
        'trading_name': 'PORVENIR AFP',
    }, {
        'counterparty_name': 'FONDO DE PENSIONES OBLIGATORIAS PORVENIR MODERADO', 'trading_name': 'PORVENIR AFP'}),
    ('FORWARD - Occidente', 'FORWARD', {
        'principal_party_name': 'BANCO DE OCCIDENTE S.A.',
        'trading_name': 'BANCO DE OCCIDENTE',
    }, {'principal_party_name': 'BANCO DE OCCIDENTE S.A.', 'trading_name': 'BANCO DE OCCIDENTE'},
    {'counterparty_name': 'BANCO DE OCCIDENTE S.A.', 'trading_name': 'BANCO DE OCCIDENTE'}),
    ('OPTIONS - identificadores', 'OPTIONS', {
        'taps_numbers': 'E03X8X',
        'account_number': '058D24EF5',
    }, {'taps_numbers': 'E03X8X', 'account_number': '058D24EF5'},
    {'taps_numbers': 'E03X8X', 'account_number': '058D24EF5'}),
]

print('VALIDACIÓN DEL MAPEO DE MORGAN STANLEY (SIN ESCRITURAS)')
for title, market, raw_values, values, expected in cases:
    issues = []
    project_canonical(entity='MORGAN_STANLEY', market_type=market,
                      raw_values=raw_values, mapping=mapping,
                      values=values, issues=issues)
    for field, want in expected.items():
        assert values.get(field) == want, f'{title}: {field}={values.get(field)!r}; esperado {want!r}'
    if market == 'OPTIONS':
        assert not values.get('taps_account_id'), 'Se asignó taps_account_id sin origen validado'
    conflicts = [item for item in issues if item.get('code') == 'CANONICAL_ALIAS_CONFLICT']
    assert not conflicts, f'{title}: conflictos: {conflicts}'
    print(f'[OK] {title}: valores independientes, 0 conflictos canónicos')
print('RESULTADO: OK. Mapeo y proyección canónica coherentes en los 3 casos de prueba.')
print('NOTA: No se han vuelto a analizar los 2.122 Excel ni actualizado filas existentes.')
