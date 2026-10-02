"""Read avails workbooks and patch only originally blank retailer ID cells.

The ZIP members and worksheet XML are retained rather than round-tripping the
whole workbook through a spreadsheet library. Styles, formulas, hidden rows,
validations, relationships, drawings and other native features remain intact.
"""
import os
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .csv_io import write_csv
from .matching import valid_id

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
PREFIX = {'MovieTMSID': 'MV', 'SeriesTMSID': 'SH', 'EpisodeTMSID': 'EP'}
CELL = re.compile(rb'<(?P<tag>(?:[\w.-]+:)?c)\b(?P<attrs>[^>]*?)(?:/>|>.*?</(?P=tag)\s*>)', re.S)
ROW = re.compile(rb'<(?P<tag>(?:[\w.-]+:)?row)\b(?P<attrs>[^>]*?)(?:/>|>.*?</(?P=tag)\s*>)', re.S)


def attribute(attrs, name):
    match = re.search(rb'\b' + name.encode() + rb'=["\']([^"\']*)["\']', attrs)
    return match[1].decode() if match else ''


def column_number(reference):
    value = 0
    for letter in re.match(r'[A-Z]+', reference)[0]:
        value = value * 26 + ord(letter) - 64
    return value


def cell_value(cell, strings):
    if cell is None:
        return ''
    formula = cell.find('s:f', NS)
    if formula is not None:
        # Even a formula with an empty cached result is an existing value.
        return '=' + (formula.text or '')
    if cell.get('t') == 'inlineStr':
        return ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
    value = cell.find('s:v', NS)
    if value is None or value.text is None:
        return ''
    if cell.get('t') == 's':
        return strings[int(value.text)]
    return value.text


def patch_cells(xml, changes):
    """Replace only target cell fragments, preserving all other XML bytes."""
    by_row = {}
    for reference, value in changes.items():
        if not re.fullmatch(r'(MV|SH|EP)\d{12}', value):
            raise ValueError(f'Invalid ID for {reference}')
        by_row.setdefault(int(re.search(r'\d+', reference)[0]), {})[reference] = value
    applied = set()
    def patch_row(match):
        number = int(attribute(match['attrs'], 'r'))
        if number not in by_row:
            return match[0]
        raw = match[0]
        tag = match['tag']
        prefix = tag.rsplit(b':', 1)[0] + b':' if b':' in tag else b''
        closing = b'</' + tag + b'>'
        for reference, value in sorted(by_row[number].items(), key=lambda item: column_number(item[0])):
            matches = list(CELL.finditer(raw))
            existing = next((m for m in matches if attribute(m['attrs'], 'r') == reference), None)
            attrs = existing['attrs'] if existing else b' r="' + reference.encode() + b'"'
            attrs = re.sub(rb'\s+t=["\'][^"\']*["\']', b'', attrs).rstrip(b'/ ').rstrip()
            cell_tag = existing['tag'] if existing else prefix + b'c'
            content = (b'<' + cell_tag + attrs + b' t="inlineStr"><' + prefix + b'is><' + prefix + b't>' +
                       value.encode('ascii') + b'</' + prefix + b't></' + prefix + b'is></' + cell_tag + b'>')
            if existing:
                raw = raw[:existing.start()] + content + raw[existing.end():]
            else:
                later = next((m for m in matches if column_number(attribute(m['attrs'], 'r')) > column_number(reference)), None)
                position = later.start() if later else raw.rfind(closing)
                if position < 0:
                    raise ValueError(f'Cannot locate row for {reference}')
                raw = raw[:position] + content + raw[position:]
            applied.add(reference)
        return raw
    result = ROW.sub(patch_row, xml)
    if applied != set(changes):
        raise ValueError('Some ID cells could not be located in the workbook')
    ET.fromstring(result)  # Never write an invalid worksheet.
    return result


class AvailsWorkbook:
    def __init__(self, filename):
        self.path = Path(filename)
        with zipfile.ZipFile(self.path) as archive:
            self.infos = archive.infolist()
            self.members = {info.filename: archive.read(info) for info in self.infos}
        if any(name.startswith('_xmlsignatures/') for name in self.members):
            raise ValueError('Digitally signed workbooks cannot be edited without invalidating their signature.')
        strings = []
        if 'xl/sharedStrings.xml' in self.members:
            strings = [''.join(t.text or '' for t in item.findall('.//s:t', NS))
                       for item in ET.fromstring(self.members['xl/sharedStrings.xml'])]
        relationships = {r.get('Id'): r.get('Target') for r in ET.fromstring(self.members['xl/_rels/workbook.xml.rels'])}
        book = ET.fromstring(self.members['xl/workbook.xml'])
        self.rows = []
        self.sheet_paths = {}
        self.targets = {}
        for sheet in book.findall('s:sheets/s:sheet', NS):
            target = relationships.get(sheet.get(RID), '')
            path = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('xl', target))
            if path not in self.members:
                continue
            root = ET.fromstring(self.members[path])
            physical_rows = root.findall('s:sheetData/s:row', NS)
            header_row, headers, kind = None, {}, None
            for row in physical_rows[:20]:
                cells = {re.match('[A-Z]+', c.get('r'))[0]: cell_value(c, strings).strip()
                         for c in row.findall('s:c', NS)}
                if 'RetailerID1' in cells.values() and any(v in cells.values() for v in ('TitleDisplayUnlimited', 'TitleInternalAlias')):
                    kind = 'Movie'
                elif all(value in cells.values() for value in ('RetailerSeriesID', 'RetailerEpisodeID1')):
                    kind = 'TV'
                else:
                    continue
                header_row = int(row.get('r'))
                if len([value for value in cells.values() if value]) != len({value for value in cells.values() if value}):
                    raise ValueError(f'Duplicate field names in {sheet.get("name")}')
                headers = {value: column for column, value in cells.items() if value}
                break
            if not kind:
                continue
            name = sheet.get('name')
            self.sheet_paths[name] = path
            for physical in physical_rows:
                number = int(physical.get('r'))
                if number <= header_row:
                    continue
                cells = {re.match('[A-Z]+', c.get('r'))[0]: c for c in physical.findall('s:c', NS)}
                def get(*labels):
                    return next((cell_value(cells.get(headers.get(label, '')), strings).strip()
                                 for label in labels if cell_value(cells.get(headers.get(label, '')), strings).strip()), '')
                title = get('TitleDisplayUnlimited', 'TitleInternalAlias') if kind == 'Movie' else get('SeriesTitleDisplayUnlimited', 'SeriesTitleInternalAlias')
                if not title:
                    continue
                record = {'Kind': kind, 'SeriesTitle': title if kind == 'TV' else '',
                          'MovieTitle': title if kind == 'Movie' else '',
                          'EpisodeTitle': get('EpisodeTitleDisplayUnlimited', 'EpisodeTitleInternalAlias') if kind == 'TV' else '',
                          'Season': get('SeasonNumber'), 'EpisodeNumber': get('EpisodeNumber'), 'Part': '',
                          'Year': get('ReleaseYear'), 'SeriesTMSID': get('RetailerSeriesID') if kind == 'TV' else '',
                          'EpisodeTMSID': get('RetailerEpisodeID1') if kind == 'TV' else '',
                          'MovieTMSID': get('RetailerID1') if kind == 'Movie' else '', 'Notes': '',
                          '_sheet': name, '_row': number, '_refs': {}, '_skip': False}
                record['_blank_fields'] = []
                mapping = {'MovieTMSID': 'RetailerID1'} if kind == 'Movie' else {
                    'SeriesTMSID': 'RetailerSeriesID', 'EpisodeTMSID': 'RetailerEpisodeID1'}
                for field, label in mapping.items():
                    reference = f'{headers[label]}{number}'
                    record['_refs'][field] = reference
                    if not record[field]:
                        self.targets[(name, reference)] = field
                        record['_blank_fields'].append(field)
                self.rows.append(record)
        if not self.rows:
            raise ValueError('No Movie or TV avails rows with the expected retailer ID headers were found.')

    def needs(self, row, field):
        return ((row['_sheet'], row['_refs'].get(field)) in self.targets and not row.get(field))

    def pending(self, row):
        return not row.get('_skip') and any(self.needs(row, field) for field in row['_refs'])

    def resume(self, filename):
        previous = AvailsWorkbook(filename)
        if self.members.keys() != previous.members.keys():
            raise ValueError('Existing output belongs to a different workbook. Choose a new output file.')
        allowed = {}
        for (sheet, reference) in self.targets:
            allowed.setdefault(self.sheet_paths[sheet], set()).add(reference)
        for path, original in self.members.items():
            old = previous.members[path]
            if path in allowed:
                def without_targets(xml):
                    return CELL.sub(lambda m: b'' if attribute(m['attrs'], 'r') in allowed[path] else m[0], xml)
                original, old = without_targets(original), without_targets(old)
            if original != old:
                raise ValueError('Existing output contains unrelated workbook changes. Choose a new output file.')
        if len(previous.rows) != len(self.rows):
            raise ValueError('Existing output has different rows. Choose a new output file.')
        identity = ('Kind', 'MovieTitle', 'SeriesTitle', 'EpisodeTitle', 'Season', 'EpisodeNumber', '_sheet', '_row')
        for row, old in zip(self.rows, previous.rows):
            if any(row[key] != old[key] for key in identity):
                raise ValueError('Existing output belongs to a different workbook. Choose a new output file.')
            for field in row['_refs']:
                if self.needs(row, field) and valid_id(old[field], PREFIX[field]):
                    row[field] = old[field].upper()

    def write(self, filename):
        output = Path(filename)
        if output.resolve() == self.path.resolve():
            raise ValueError('Choose a different output file to preserve the input workbook.')
        output.parent.mkdir(parents=True, exist_ok=True)
        changes = {}
        for row in self.rows:
            for field, reference in row['_refs'].items():
                if (row['_sheet'], reference) in self.targets and valid_id(row.get(field), PREFIX[field]):
                    changes.setdefault(self.sheet_paths[row['_sheet']], {})[reference] = row[field].upper()
        members = dict(self.members)
        for path, values in changes.items():
            members[path] = patch_cells(members[path], values)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as stream:
                temp_name = stream.name
            with zipfile.ZipFile(temp_name, 'w') as archive:
                for info in self.infos:
                    archive.writestr(info, members[info.filename])
            with open(temp_name, 'rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temp_name, output)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
        report = []
        for row in self.rows:
            report.append({'Sheet': row['_sheet'], 'Row': row['_row'], 'Type': row['Kind'],
                           'MovieTitle': row['MovieTitle'], 'SeriesTitle': row['SeriesTitle'],
                           'EpisodeTitle': row['EpisodeTitle'], 'Season': row['Season'],
                           'EpisodeNumber': row['EpisodeNumber'], 'MovieTMSID': row['MovieTMSID'],
                           'SeriesTMSID': row['SeriesTMSID'], 'EpisodeTMSID': row['EpisodeTMSID'],
                           'Status': 'Skipped' if row['_skip'] else ('Needs review' if self.pending(row) else 'Complete'),
                           'Notes': row['Notes']})
        write_csv(output.with_name(output.stem + '_lookup_report.csv'), report, list(report[0]))

    def summary(self):
        filled = sum(valid_id(row[field], PREFIX[field]) for row in self.rows for field, reference in row['_refs'].items()
                     if (row['_sheet'], reference) in self.targets)
        return {'filled': filled, 'remaining': len(self.targets) - filled,
                'found': filled, 'review': sum(self.pending(row) for row in self.rows),
                'skipped': sum(row['_skip'] for row in self.rows)}


def preview(filename):
    path = Path(filename)
    if path.suffix.lower() == '.xlsx':
        return AvailsWorkbook(path).rows
    from .csv_io import read_csv
    return read_csv(path)
