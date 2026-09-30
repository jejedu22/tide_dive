// Export Excel (.xlsx) côté navigateur, sans dépendance : un classeur d'une
// feuille, écrit en SpreadsheetML et emballé dans un zip non compressé.
//
//   XlsxExport.download("fichier.xlsx", "Feuille", columns, rows)
//
// columns : [{ header, type, width, value: row => … }]
//   type : "text" (défaut), "int", "decimal" (0,00), "date" (AAAA-MM-JJ),
//          "time" (HH:MM). Les dates et heures deviennent de vraies valeurs
//          Excel (triables, calculables). null / undefined / "" : cellule vide.

const XlsxExport = (() => {
  const XML_HEAD = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n`;
  const NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
  const NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
  const NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships";

  // Styles (index dans cellXfs) : 0 normal, 1 en-tête, 2 date, 3 heure, 4 décimal
  const STYLE = { header: 1, date: 2, time: 3, decimal: 4 };
  const STYLES = XML_HEAD + `<styleSheet xmlns="${NS_MAIN}">
<numFmts count="2"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/><numFmt numFmtId="165" formatCode="hh:mm"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD9EEF2"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>`;

  // Caractères interdits en XML 1.0 retirés, puis échappement
  function xmlText(s) {
    return String(s)
      .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g, "")
      .replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  function colName(i) {
    let s = "";
    for (i++; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + (i - 1) % 26) + s;
    return s;
  }

  // Numéro de série Excel : jours depuis le 30/12/1899 (calcul en UTC, sans fuseau)
  function dateSerial(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    return m ? (Date.UTC(+m[1], m[2] - 1, +m[3]) - Date.UTC(1899, 11, 30)) / 86400000 : null;
  }

  function timeSerial(hhmm) {
    const m = /^(\d{1,2}):(\d{2})/.exec(hhmm || "");
    return m ? (+m[1] * 60 + +m[2]) / 1440 : null;
  }

  function cell(ref, type, v) {
    if (v == null || v === "") return "";
    let n = null, style = 0;
    switch (type) {
      case "date": n = dateSerial(v); style = STYLE.date; break;
      case "time": n = timeSerial(v); style = STYLE.time; break;
      case "decimal": n = Number(v); style = STYLE.decimal; break;
      case "int": n = Number(v); break;
    }
    if (type && type !== "text") {
      // valeur non convertible : on la garde telle quelle en texte
      if (n != null && Number.isFinite(n)) return `<c r="${ref}"${style ? ` s="${style}"` : ""}><v>${n}</v></c>`;
    }
    return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${xmlText(v)}</t></is></c>`;
  }

  function sheetXml(columns, rows) {
    const last = colName(columns.length - 1);
    const cols = columns.map((c, i) =>
      `<col min="${i + 1}" max="${i + 1}" width="${c.width || 12}" customWidth="1"/>`).join("");
    const head = `<row r="1">${columns.map((c, i) =>
      `<c r="${colName(i)}1" t="inlineStr" s="${STYLE.header}"><is><t xml:space="preserve">${xmlText(c.header)}</t></is></c>`).join("")}</row>`;
    const body = rows.map((row, r) =>
      `<row r="${r + 2}">${columns.map((c, i) => cell(`${colName(i)}${r + 2}`, c.type, c.value(row))).join("")}</row>`).join("");
    // en-tête figé + filtre automatique sur toutes les colonnes
    return XML_HEAD + `<worksheet xmlns="${NS_MAIN}" xmlns:r="${NS_REL}">
<dimension ref="A1:${last}${rows.length + 1}"/>
<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
<sheetFormatPr defaultRowHeight="15"/>
<cols>${cols}</cols>
<sheetData>${head}${body}</sheetData>
<autoFilter ref="A1:${last}${rows.length + 1}"/>
</worksheet>`;
  }

  // Nom de feuille Excel : 31 caractères max, sans []:*?/\
  const sheetName = s => (String(s).replace(/[[\]:*?/\\]/g, " ").trim().slice(0, 31) || "Feuille1");

  function workbookFiles(name, columns, rows) {
    return {
      "[Content_Types].xml": XML_HEAD + `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>`,
      "_rels/.rels": XML_HEAD + `<Relationships xmlns="${NS_PKG}">
<Relationship Id="rId1" Type="${NS_REL}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>`,
      "xl/workbook.xml": XML_HEAD + `<workbook xmlns="${NS_MAIN}" xmlns:r="${NS_REL}">
<sheets><sheet name="${xmlText(sheetName(name))}" sheetId="1" r:id="rId1"/></sheets>
<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'${xmlText(sheetName(name).replace(/'/g, "''"))}'!$A$1:$${colName(columns.length - 1)}$${rows.length + 1}</definedName></definedNames>
</workbook>`,
      "xl/_rels/workbook.xml.rels": XML_HEAD + `<Relationships xmlns="${NS_PKG}">
<Relationship Id="rId1" Type="${NS_REL}/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="${NS_REL}/styles" Target="styles.xml"/>
</Relationships>`,
      "xl/styles.xml": STYLES,
      "xl/worksheets/sheet1.xml": sheetXml(columns, rows),
    };
  }

  // ---- Zip (méthode « stored », sans compression) ----

  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    let c = 0xFFFFFFFF;
    for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  function zip(files) {
    const enc = new TextEncoder();
    const now = new Date();
    const dosTime = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
    const dosDate = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
    const parts = [], central = [];
    let offset = 0;

    for (const [name, content] of Object.entries(files)) {
      const nameBytes = enc.encode(name);
      const data = enc.encode(content);
      const crc = crc32(data);

      const local = new DataView(new ArrayBuffer(30));
      local.setUint32(0, 0x04034b50, true);
      local.setUint16(4, 20, true);         // version requise
      local.setUint16(6, 0x0800, true);     // noms en UTF-8
      local.setUint16(8, 0, true);          // stored
      local.setUint16(10, dosTime, true);
      local.setUint16(12, dosDate, true);
      local.setUint32(14, crc, true);
      local.setUint32(18, data.length, true);
      local.setUint32(22, data.length, true);
      local.setUint16(26, nameBytes.length, true);
      local.setUint16(28, 0, true);
      parts.push(local, nameBytes, data);

      const dir = new DataView(new ArrayBuffer(46));
      dir.setUint32(0, 0x02014b50, true);
      dir.setUint16(4, 20, true);
      dir.setUint16(6, 20, true);
      dir.setUint16(8, 0x0800, true);
      dir.setUint16(10, 0, true);
      dir.setUint16(12, dosTime, true);
      dir.setUint16(14, dosDate, true);
      dir.setUint32(16, crc, true);
      dir.setUint32(20, data.length, true);
      dir.setUint32(24, data.length, true);
      dir.setUint16(28, nameBytes.length, true);
      dir.setUint32(42, offset, true);      // autres champs à 0
      central.push(dir, nameBytes);

      offset += 30 + nameBytes.length + data.length;
    }

    const dirSize = central.reduce((n, p) => n + p.byteLength, 0);
    const end = new DataView(new ArrayBuffer(22));
    const count = Object.keys(files).length;
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, count, true);
    end.setUint16(10, count, true);
    end.setUint32(12, dirSize, true);
    end.setUint32(16, offset, true);
    return [...parts, ...central, end];
  }

  const MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

  function build(name, columns, rows) {
    return new Blob(zip(workbookFiles(name, columns, rows)), { type: MIME });
  }

  function download(filename, name, columns, rows) {
    const url = URL.createObjectURL(build(name, columns, rows));
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // Nom de fichier sûr : lettres, chiffres, tirets
  function slug(s) {
    return String(s).normalize("NFD").replace(/[̀-ͯ]/g, "")
      .replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() || "export";
  }

  return { build, download, slug };
})();
