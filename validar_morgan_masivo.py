"""Auditoría de proyecciones de Morgan Stanley (solo lectura).

Uso:
 python validar_morgan_masivo.py "H:\\ruta\\historicos" ".\\historical_scan_output_v2\\historical_structure_inventory.csv"

Leerá los archivos originales y el inventario; NO abre conexiones ni escribe en PostgreSQL.
"""
from __future__ import annotations
import argparse
import csv
from collections import Counter
from pathlib import Path

from historical_import import (
    load_mapping, resolve_entity, resolve_market, is_ignored_sheet,
    row_source_cells, project_canonical, _blank,
    _looks_like_instruction_row, _looks_like_repeated_header,
)
from historical_reader import read_inventory, group_by_file, iter_sheet_rows, load_report_passwords


def run():
    p = argparse.ArgumentParser(description='Valida todos los archivos de Morgan Stanley, sin escribir en BD')
    p.add_argument('root', type=Path)
    p.add_argument('inventory_csv', type=Path)
    p.add_argument('--map', dest='map_path', type=Path, default=Path('historical_schema_map.json'))
    p.add_argument('--output-prefix', default='validacion_morgan_masiva')
    p.add_argument('--min-year', type=int, default=2018)
    p.add_argument('--max-year', type=int, default=2026)
    args = p.parse_args()
    root, inventory = args.root.resolve(), args.inventory_csv.resolve()
    if not root.is_dir(): p.error(f'No existe carpeta: {root}')
    if not inventory.is_file(): p.error(f'No existe inventario: {inventory}')
    if not args.map_path.is_file(): p.error(f'No existe mapeo: {args.map_path}')
    mapping = load_mapping(args.map_path)
    passwords = load_report_passwords()
    grouped = group_by_file(read_inventory(inventory))
    candidates = [(rel, sheets) for rel, sheets in grouped.items()
                  if sheets[0].source_year is not None
                  and args.min_year <= sheets[0].source_year <= args.max_year
                  and resolve_entity(sheets[0], mapping) == 'MORGAN_STANLEY']
    candidates.sort(key=lambda x: x[0].lower())
    if not candidates: p.error('Sin archivos de MORGAN_STANLEY en el inventario')
    prefix = Path(args.output_prefix)
    detailed_path = prefix.with_name(prefix.name + '_archivos.csv')
    conflict_path = prefix.with_name(prefix.name + '_conflictos.csv')
    summary = Counter()
    detail = []
    conflict_counts = Counter()
    conflict_files = {}
    print(f'Analizando {len(candidates)} archivos de Morgan Stanley. Solo lectura.', flush=True)
    for index, (rel, sheets) in enumerate(candidates, 1):
        first = sheets[0]
        row_count = 0
        per_file = Counter()
        file_issues = Counter()
        state = 'ok'
        note = ''
        ok_sheets = [s for s in sheets if s.scan_status == 'OK']
        if not ok_sheets:
            state = first.scan_status
        else:
            try:
                for sheet in ok_sheets:
                    if is_ignored_sheet('MORGAN_STANLEY', sheet, mapping):
                        continue
                    market = resolve_market('MORGAN_STANLEY', sheet, mapping)
                    if not market:
                        file_issues['unsupported_sheets'] += 1
                        continue
                    for row in iter_sheet_rows(root, sheet, 'MORGAN_STANLEY', passwords):
                        if not any(not _blank(v) for v in row):
                            continue
                        if _looks_like_instruction_row(row) or _looks_like_repeated_header(
                            entity='MORGAN_STANLEY', sheet=sheet, row=row, mapping=mapping
                        ):
                            continue
                        cells = row_source_cells(sheet, row)
                        if not cells:
                            continue
                        raw_values = {str(c['normalized_header']).strip(): c['value']
                                      for c in cells if str(c['normalized_header']).strip()}
                        values = {}
                        common_aliases = mapping.get('common_column_aliases', {})
                        entity_aliases = mapping.get('entity_column_aliases', {}).get('MORGAN_STANLEY', {})
                        allowed = set(mapping['columns']['MORGAN_STANLEY'].keys())
                        for src, raw in raw_values.items():
                            base = common_aliases.get(src, src)
                            dest = entity_aliases.get(base, entity_aliases.get(src, base))
                            if dest not in allowed:
                                continue
                            if dest in values and not _blank(values[dest]) and not _blank(raw):
                                continue
                            values[dest] = raw
                        issues = []
                        project_canonical(entity='MORGAN_STANLEY', market_type=market,
                                          raw_values=raw_values, mapping=mapping,
                                          values=values, issues=issues)
                        row_count += 1
                        per_file[market] += 1
                        for issue in issues:
                            if issue.get('code') != 'CANONICAL_ALIAS_CONFLICT':
                                continue
                            key = (market, issue.get('field',''), issue.get('source_column',''))
                            conflict_counts[key] += 1
                            file_issues['canonical_alias_conflict'] += 1
                            conflict_files.setdefault(key, set()).add(rel)
            except Exception as exc:
                state = 'read_error'
                note = f'{type(exc).__name__}: {exc}'[:450]
        summary['files_seen'] += 1
        summary['rows_evaluated'] += row_count
        summary['canonical_conflicts'] += file_issues['canonical_alias_conflict']
        if file_issues['canonical_alias_conflict']:
            summary['files_with_conflicts'] += 1
        if state != 'ok': summary['files_with_read_issue'] += 1
        detail.append({'relative_path': rel, 'filename': first.filename,
                       'year': first.source_year, 'status': state,
                       'rows_evaluated': row_count,
                       'forward_rows': per_file['FORWARD'], 'options_rows': per_file['OPTIONS'],
                       'canonical_conflicts': file_issues['canonical_alias_conflict'],
                       'unsupported_sheets': file_issues['unsupported_sheets'], 'note': note})
        if index % 50 == 0 or index == len(candidates):
            print(f'[{index}/{len(candidates)}] filas={summary["rows_evaluated"]} conflictos={summary["canonical_conflicts"]} archivos_con_error_lectura={summary["files_with_read_issue"]}',flush=True)
    fields = ['relative_path','filename','year','status','rows_evaluated',
              'forward_rows','options_rows','canonical_conflicts','unsupported_sheets','note']
    with detailed_path.open('w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(detail)
    with conflict_path.open('w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh); w.writerow(['market_type','target_field','source_column','occurrences','files_affected'])
        for (market, field, source), n in sorted(conflict_counts.items(), key=lambda x:-x[1]):
            w.writerow([market, field, source, n, len(conflict_files[(market, field, source)])])
    print('\nRESUMEN DE VALIDACIÓN MASIVA (SIN ESCRITURAS)')
    for field in ('files_seen','rows_evaluated','canonical_conflicts','files_with_conflicts','files_with_read_issue'):
        print(f'{field}: {summary[field]}')
    print(f'Detalle: {detailed_path}')
    print(f'Conflictos: {conflict_path}')
    print('Referencia histórica (BD, antes del cambio): 7016 conflictos en 349 archivos.')
    print('ATENCIÓN: Esta auditoría evalúa solo conflictos CANONICAL_ALIAS_CONFLICT. No mide conversiones, duplicados ni errores de inserción.')


if __name__ == '__main__':
    run()
