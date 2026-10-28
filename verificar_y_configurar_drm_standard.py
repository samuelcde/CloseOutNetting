"""Audita DRM/IRM en archivos STANDARD y prepara el lector de CloseOutNetting.

No conecta a PostgreSQL ni descifra documentos. Dependencia: olefile.
Uso:
 python verificar_y_configurar_drm_standard.py auditar ROOT INVENTARIO
 python verificar_y_configurar_drm_standard.py instalar
 python verificar_y_configurar_drm_standard.py actualizar-inventario ROOT INVENTARIO
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

TARGET_YEAR = '2022'
TARGET_ENTITY = 'STANDARD'
TARGET_NAME_FRAGMENT = 'Option A_Fields Derivatives'
OLE_MAGIC = bytes.fromhex('d0cf11e0a1b11ae1')

def is_target(row):
    return (row.get('source_year','').strip() == TARGET_YEAR
            and row.get('entity_hint','').strip().upper() == TARGET_ENTITY
            and TARGET_NAME_FRAGMENT.lower() in row.get('filename','').lower()
            and row.get('scan_status','').strip().upper() == 'OPEN_ERROR')

def audit(root, inv):
    try:
        import olefile
    except ImportError:
        sys.exit('Falta olefile. Ejecute: python -m pip install olefile')
    with inv.open(encoding='utf-8-sig', newline='') as f:
        inventory = list(csv.DictReader(f))
    matches = {}
    for row in inventory:
        if is_target(row):
            matches.setdefault(row['relative_path'], row)
    report = []
    for rel, row in sorted(matches.items()):
        file = root / Path(rel)
        item = {'relative_path':rel, 'filename':row['filename'], 'year':row['source_year'],
                'result':'', 'detail':'', 'sha256':row.get('file_sha256','')}
        try:
            if not file.is_file():
                item['result']='ARCHIVO_NO_ENCONTRADO'
            else:
                with file.open('rb') as f:
                    magic=f.read(8)
                if magic != OLE_MAGIC:
                    item['result']='NO_OLE_CFBF'
                    item['detail']='Firma: '+magic.hex(' ')
                else:
                    with olefile.OleFileIO(str(file)) as ole:
                        streams=['/'.join(x) for x in ole.listdir(streams=True,storages=False)]
                    names=set(streams)
                    drm_space=any('DRMEncryptedDataSpace' in x for x in streams)
                    drm_transform=any('DRMEncryptedTransform' in x for x in streams)
                    encrypted='EncryptedPackage' in names
                    regular='EncryptionInfo' in names
                    if drm_space and drm_transform and encrypted:
                        item['result']='DRM_PROTECTED'
                    elif regular and encrypted:
                        item['result']='OFFICE_PASSWORD_ENCRYPTED'
                    elif 'Workbook' in names or 'Book' in names:
                        item['result']='XLS_BINARIO'
                    else:
                        item['result']='OLE_OTRO'
                    item['detail']='; '.join(streams[:30])
        except Exception as exc:
            item['result']='ERROR_INSPECCION'
            item['detail']=f'{type(exc).__name__}: {exc}'
        report.append(item)
        print(f"[{len(report)}/{len(matches)}] {item['filename']}: {item['result']}")
    output = Path('diagnostico_drm_standard.csv')
    with output.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['relative_path','filename','year','result','detail','sha256'])
        w.writeheader();w.writerows(report)
    counts=Counter(x['result'] for x in report)
    print('\nRESUMEN:',dict(counts),'TOTAL:',len(report))
    print('Reporte:',output.resolve())
    if len(report)!=15:
        print('ATENCIÓN: se esperaban 15 archivos OPEN_ERROR. Compruebe el inventario y filtros.')
    return report

def backup(path):
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    dest=path.with_name(path.name+'.bak_'+stamp)
    shutil.copy2(path,dest)
    print('Respaldo:',dest)

def install():
    scanner=Path('historical_structure_scanner.py')
    importer=Path('historical_import.py')
    if not scanner.exists() or not importer.exists():
        sys.exit('Ejecute desde la raíz del proyecto: faltan historical_structure_scanner.py o historical_import.py')
    source=scanner.read_text(encoding='utf-8')
    hook='    drm_result = _detect_drm_protection(path)\n'
    if hook not in source:
        marker='    suffix = path.suffix.lower()\n'
        if source.count(marker)!=1:
            sys.exit('No se encontró un punto de inserción único en scan_file; no se modificó nada.')
        helper='''\n\ndef _detect_drm_protection(path: Path) -> list[SheetScan] | None:\n    """Reconoce Office DRM/IRM por estructuras OLE; no intenta descifrar."""\n    try:\n        with path.open("rb") as handle:\n            if handle.read(8) != bytes.fromhex("d0cf11e0a1b11ae1"):\n                return None\n        import olefile\n        with olefile.OleFileIO(str(path)) as ole:\n            streams = {"/".join(parts) for parts in ole.listdir(streams=True, storages=False)}\n        if ("EncryptedPackage" in streams\n            and any("DRMEncryptedDataSpace" in name for name in streams)\n            and any("DRMEncryptedTransform" in name for name in streams)):\n            return [SheetScan(\n                sheet_name="", header_row=None, headers=[], normalized_headers=[],\n                structure_hash=None, data_rows_estimate=None,\n                scan_status="DRM_PROTECTED", open_method="OLE_DRM",\n                error="Documento Office protegido por DRM/IRM; requiere acceso autorizado."\n            )]\n    except ImportError:\n        return None  # La auditoría separada sí exige olefile.\n    except Exception:\n        return None  # Mantener el manejo preexistente del escáner.\n    return None\n\n'''
        point='def scan_file(\n'
        if source.count(point)!=1:
            sys.exit('No se encontró una sola definición de scan_file; no se modificó nada.')
        source=source.replace(point,helper+point,1)
        source=source.replace(marker,'''    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:\n        drm_result = _detect_drm_protection(path)\n        if drm_result is not None:\n            return drm_result\n\n'''+marker,1)
        compile(source,str(scanner),'exec')
        backup(scanner)
        scanner.write_text(source,encoding='utf-8')
        print('Escáner actualizado:',scanner)
    else:
        print('Escáner: detección ya instalada')
    s=importer.read_text(encoding='utf-8')
    needle='        "OPEN_ERROR": "open_error",'
    addition='        "DRM_PROTECTED": "drm_protected",\n'
    if addition not in s:
        if s.count(needle)!=1:
            sys.exit('No se encontró el mapa de estados esperado en historical_import.py')
        s=s.replace(needle,addition+needle,1)
        compile(s,str(importer),'exec')
        backup(importer)
        importer.write_text(s,encoding='utf-8')
        print('Importador actualizado:',importer)
    else:
        print('Importador: estado ya incorporado')

def update_inventory(root,inv):
    results=audit(root,inv)
    confirmed={x['relative_path'] for x in results if x['result']=='DRM_PROTECTED'}
    if not confirmed:
        sys.exit('No hay archivos DRM confirmados; inventario sin cambios.')
    with inv.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        fields=reader.fieldnames
        rows=list(reader)
    count=0
    for row in rows:
        if is_target(row) and row['relative_path'] in confirmed:
            row['scan_status']='DRM_PROTECTED'
            row['open_method']='OLE_DRM'
            row['error']='Documento Office protegido por DRM/IRM; requiere acceso autorizado.'
            count+=1
    if count!=len(confirmed):
        sys.exit('Cantidad de filas no coincide con confirmados; no se actualizó inventario')
    backup(inv)
    tmp=inv.with_suffix('.csv.tmp')
    with tmp.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields)
        w.writeheader();w.writerows(rows)
    tmp.replace(inv)
    print('Inventario actualizado, archivos DRM:',count)
    print('No se modificaron los Excel ni PostgreSQL.')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['auditar','instalar','actualizar-inventario'])
    p.add_argument('root',nargs='?',type=Path)
    p.add_argument('inventory',nargs='?',type=Path)
    a=p.parse_args()
    if a.action=='instalar':install()
    elif a.root is None or a.inventory is None:
        p.error('auditar y actualizar-inventario requieren ROOT e INVENTARIO')
    elif a.action=='auditar':audit(a.root,a.inventory)
    else:update_inventory(a.root,a.inventory)
