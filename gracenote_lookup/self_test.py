"""Synthetic, already completed workbook for the packaged startup check."""
from zipfile import ZipFile
from xml.sax.saxutils import escape


def create_workbook(path):
    namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    def sheet(headers, values):
        content = ''
        for row, cells in enumerate([headers, values], 2):
            content += f'<row r="{row}">'
            for col, value in enumerate(cells):
                content += f'<c r="{chr(65+col)}{row}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            content += '</row>'
        return f'<worksheet xmlns="{namespace}"><sheetData>{content}</sheetData></worksheet>'
    with ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Movie" sheetId="1" r:id="rId1"/><sheet name="TV" sheetId="2" r:id="rId2"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', sheet(['TitleDisplayUnlimited', 'ReleaseYear', 'RetailerID1'], ['Example Movie', '2021', 'MV000000000001']))
        archive.writestr('xl/worksheets/sheet2.xml', sheet(['SeriesTitleDisplayUnlimited', 'EpisodeTitleDisplayUnlimited', 'SeasonNumber', 'RetailerSeriesID', 'RetailerEpisodeID1'], ['Example Series', 'The Pilot', '1', 'SH000000000001', 'EP000000000001']))
