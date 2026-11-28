// @vitest-environment node
import { access, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import { OFFICE_PREVIEW_CLEANUP_TIMEOUT_MS, OFFICE_PREVIEW_CONVERSION_TIMEOUT_MS, openOfficePreview } from './office-preview'

const TEST_OVERHEAD_PER_ATTEMPT_MS = 15_000

function officePreviewTestTimeout(attempts: number): number {
  const attemptTimeoutMs = OFFICE_PREVIEW_CONVERSION_TIMEOUT_MS + OFFICE_PREVIEW_CLEANUP_TIMEOUT_MS + TEST_OVERHEAD_PER_ATTEMPT_MS
  return attempts * attemptTimeoutMs
}

test('legacy and macro Office files are rejected before any application is opened', async () => {
  const directory = await testDirectory('office-preview-unsupported-')
  const sourcePath = join(directory, 'unsafe.docm')
  await writeFile(sourcePath, 'synthetic macro-format input')
  await expect(openOfficePreview({ sourcePath, directory: join(directory, 'preview') }))
    .rejects.toThrow('Office preview supports only .docx, .xlsx and .pptx')
})

function officePackage(entries: Record<string, string>): Buffer {
  const files: Buffer[] = []
  const central: Buffer[] = []
  let offset = 0
  for (const [name, text] of Object.entries(entries)) {
    const nameBytes = Buffer.from(name)
    const data = Buffer.from(text)
    let checksum = 0xffffffff
    for (const byte of data) {
      checksum ^= byte
      for (let bit = 0; bit < 8; bit++) checksum = (checksum >>> 1) ^ (0xedb88320 & -(checksum & 1))
    }
    checksum = (checksum ^ 0xffffffff) >>> 0
    const local = Buffer.alloc(30); local.writeUInt32LE(0x04034b50); local.writeUInt16LE(20, 4)
    local.writeUInt32LE(checksum, 14); local.writeUInt32LE(data.length, 18); local.writeUInt32LE(data.length, 22); local.writeUInt16LE(nameBytes.length, 26)
    files.push(local, nameBytes, data)
    const header = Buffer.alloc(46); header.writeUInt32LE(0x02014b50); header.writeUInt16LE(20, 4); header.writeUInt16LE(20, 6)
    header.writeUInt32LE(checksum, 16); header.writeUInt32LE(data.length, 20); header.writeUInt32LE(data.length, 24); header.writeUInt16LE(nameBytes.length, 28); header.writeUInt32LE(offset, 42)
    central.push(header, nameBytes)
    offset += local.length + nameBytes.length + data.length
  }
  const directory = Buffer.concat(central)
  const end = Buffer.alloc(22); end.writeUInt32LE(0x06054b50); end.writeUInt16LE(central.length / 2, 8); end.writeUInt16LE(central.length / 2, 10); end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16)
  return Buffer.concat([...files, directory, end])
}

const minimalWord = {
  '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
  '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
  'word/document.xml': '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>本机只读预览 Word</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>',
}

test.runIf(process.platform === 'win32')('a real fixed DOCX snapshot becomes a local read-only PDF and can be released', async () => {
  const directory = await testDirectory('office-preview-word-')
  const sourcePath = join(directory, 'sample.docx')
  const original = officePackage(minimalWord)
  await writeFile(sourcePath, original)
  const preview = await openOfficePreview({ sourcePath, directory: join(directory, 'preview') })
  expect(preview.sections.map(section => section.label)).toEqual(['文档'])
  expect((await readFile(preview.sections[0].pdfPath)).subarray(0, 5).toString()).toBe('%PDF-')
  expect(await readFile(sourcePath)).toEqual(original)
  await preview.close()
  await expect(access(join(directory, 'preview'))).rejects.toThrow()
}, officePreviewTestTimeout(1))

test.runIf(process.platform === 'win32')('real workbook preview exposes both visible sheets and excludes hidden sheets', async () => {
  const directory = await testDirectory('office-preview-workbook-')
  const sourcePath = join(directory, 'sample.xlsx')
  const original = officePackage({
    '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="销售" sheetId="1" r:id="rId1"/><sheet name="明细" sheetId="2" r:id="rId2"/><sheet name="隐藏" state="hidden" sheetId="3" r:id="rId3"/></sheets></workbook>',
    'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/></Relationships>',
    ...Object.fromEntries([1, 2, 3].map(index => [`xl/worksheets/sheet${index}.xml`, '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>合成工作表</t></is></c></row></sheetData></worksheet>'])),
  })
  await writeFile(sourcePath, original)
  const preview = await openOfficePreview({ sourcePath, directory: join(directory, 'preview') })
  expect(preview.sections.map(section => section.label)).toEqual(['销售', '明细'])
  for (const section of preview.sections) expect((await readFile(section.pdfPath)).subarray(0, 5).toString()).toBe('%PDF-')
  expect(await readFile(sourcePath)).toEqual(original)
  await preview.close()
}, officePreviewTestTimeout(1))


test.runIf(process.platform === 'win32')('real slides retain separate local pages including an embedded chart', async () => {
  const directory = await testDirectory('office-preview-slides-')
  const sourcePath = join(directory, 'sample.pptx')
  const original = await readFile(new URL('../../scripts/office-preview-fixtures/slides-layout.pptx', import.meta.url))
  await writeFile(sourcePath, original)
  const preview = await openOfficePreview({ sourcePath, directory: join(directory, 'preview') })
  try {
    expect(preview.sections.map(section => section.label)).toEqual(['幻灯片 1', '幻灯片 2'])
    for (const section of preview.sections) expect((await readFile(section.pdfPath)).subarray(0, 5).toString()).toBe('%PDF-')
    expect(await readFile(sourcePath)).toEqual(original)
  } finally {
    await preview.close()
  }
}, officePreviewTestTimeout(1))

test.runIf(process.platform === 'win32')('disguised macro content is rejected and the failed preview directory is released', async () => {
  const directory = await testDirectory('office-preview-macro-')
  const sourcePath = join(directory, 'sample.docx')
  await writeFile(sourcePath, officePackage({ ...minimalWord, 'word/vbaProject.bin': 'synthetic disabled payload' }))
  await expect(openOfficePreview({ sourcePath, directory: join(directory, 'preview') })).rejects.toThrow('rejects macros')
  await expect(access(join(directory, 'preview'))).rejects.toThrow()
}, officePreviewTestTimeout(1))

test.runIf(process.platform === 'win32')('external relationships are rejected including XML encoded attribute values', async () => {
  const directory = await testDirectory('office-preview-external-')
  const sourcePath = join(directory, 'sample.docx')
  await writeFile(sourcePath, officePackage({ ...minimalWord, 'word/_rels/document.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="https://example.invalid/never-fetch.png" TargetMode="Exter&#110;al"/></Relationships>' }))
  await expect(openOfficePreview({ sourcePath, directory: join(directory, 'preview') })).rejects.toThrow('rejects macros and external links')
}, officePreviewTestTimeout(1))

test.runIf(process.platform === 'win32')('cancelling a real Office conversion releases its instance and preview directory', async () => {
  const directory = await testDirectory('office-preview-cancel-')
  const sourcePath = join(directory, 'sample.docx')
  await writeFile(sourcePath, officePackage(minimalWord))
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 1500)
  try {
    await expect(openOfficePreview({ sourcePath, directory: join(directory, 'preview'), signal: controller.signal })).rejects.toThrow('Office preview cancelled')
  } finally {
    clearTimeout(timer)
  }
  await expect(access(join(directory, 'preview'))).rejects.toThrow()
  const nextPreview = await openOfficePreview({ sourcePath, directory: join(directory, 'next-preview') })
  try {
    expect(nextPreview.sections[0].label).toBe('文档')
  } finally {
    await nextPreview.close()
  }
}, officePreviewTestTimeout(2))
