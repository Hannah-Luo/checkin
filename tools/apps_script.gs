var SHARED_SECRET = 'change-me-to-something-random';

var RESPONSES_TAB = '';

var MATRIC_NAMES = ['matricutionnumber', 'matriculationnumber', 'matricnumber',
                    'matric'];
var SHOWUP_NAMES = ['showup', 'shownup', 'attended'];
var WALKIN_NAMES = ['walkin'];
var SLOT_NAMES   = ['allocatedtimeslot', 'allocatedsession', 'allocatedslot'];
var TABLE_NAMES  = ['table', 'tablenumber', 'tableno', 'allocatedtable',
                    'tableallocated'];

function normaliseHeader(h) {
  return String(h == null ? '' : h).toLowerCase().replace(/[^a-z0-9]/g, '');
}

function findColumn(headers, names, role) {
  var hits = [];
  for (var c = 0; c < headers.length; c++) {
    var n = normaliseHeader(headers[c]);
    if (n && names.indexOf(n) !== -1) hits.push(c);
  }
  if (hits.length > 1) {
    var which = hits.map(function (c) { return '"' + headers[c] + '" (col ' + (c + 1) + ')'; });
    throw new Error('two columns both look like ' + role + ': ' + which.join(' and ') +
                    ' -- rename one, I will not guess');
  }
  return hits.length ? hits[0] : -1;
}

function writeCell(sheet, headers, col, names, row, value) {
  var n = normaliseHeader(headers[col]);
  if (names.indexOf(n) === -1) {
    throw new Error('refusing to write to column ' + (col + 1) +
                    ' ("' + String(headers[col]).slice(0, 60) + '")');
  }
  sheet.getRange(row, col + 1).setValue(value);
}

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    if (String(body.secret).trim() !== String(SHARED_SECRET).trim()) {
      return reply({ok: false, error: 'bad secret'});
    }
    var rows = body.rows || [];
    if (!rows.length) return reply({ok: true, updated: 0, missing: []});

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = RESPONSES_TAB ? ss.getSheetByName(RESPONSES_TAB) : ss.getSheets()[0];
    if (!sheet) return reply({ok: false, error: 'responses tab not found'});

    var lock = LockService.getScriptLock();
    lock.waitLock(20000);
    try {
      var result = applyRows(sheet, rows);
      result.ok = true;
      return reply(result);
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    return reply({ok: false, error: String(err)});
  }
}

function doGet() {
  return reply({ok: true, service: 'ntu-checkin-writeback'});
}

function applyRows(sheet, rows) {
  var values = sheet.getDataRange().getValues();
  var headers = values[0];

  var matricCol = findColumn(headers, MATRIC_NAMES, 'the matric number');
  var showupCol = findColumn(headers, SHOWUP_NAMES, 'Show-up');
  var walkinCol = findColumn(headers, WALKIN_NAMES, 'Walk-in');
  var slotCol   = findColumn(headers, SLOT_NAMES, 'Allocated Time Slot');
  var tableCol  = findColumn(headers, TABLE_NAMES, 'Table');

  if (matricCol < 0) {
    throw new Error('no column is exactly a matric number. Headers seen: ' +
                    headerList(headers));
  }
  if (showupCol < 0) {
    throw new Error('no column is exactly "Show-up". Headers seen: ' +
                    headerList(headers));
  }

  var index = {};
  for (var r = 1; r < values.length; r++) {
    var key = normaliseMatric(values[r][matricCol]);
    if (key && !(key in index)) index[key] = r + 1;
  }

  var updated = 0, walkIns = 0, skippedWalkIns = 0;
  var slots = 0, skippedSlots = 0;
  var tables = 0, cleared = 0, skippedTables = 0;
  var missing = [];

  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var target = index[normaliseMatric(row.matric)];
    if (!target) {
      missing.push(row.matric);
      continue;
    }
    writeCell(sheet, headers, showupCol, SHOWUP_NAMES, target,
              row.show_up === 0 ? 0 : 1);
    updated++;

    var walkIn = (row.walk_in === 0 || row.walk_in) ? String(row.walk_in) : '';
    if (walkIn) {
      if (walkinCol >= 0) {
        writeCell(sheet, headers, walkinCol, WALKIN_NAMES, target, row.walk_in);
        walkIns++;
      } else {
        skippedWalkIns++;
      }
    }

    var slot = (row.slot === 0 || row.slot) ? String(row.slot) : '';
    if (slot) {
      if (slotCol >= 0) {
        writeCell(sheet, headers, slotCol, SLOT_NAMES, target, row.slot);
        slots++;
      } else {
        skippedSlots++;
      }
    }

    if (Object.prototype.hasOwnProperty.call(row, 'table')) {
      var table = (row.table === 0 || row.table) ? String(row.table) : '';
      if (tableCol >= 0) {
        writeCell(sheet, headers, tableCol, TABLE_NAMES, target,
                  table ? row.table : '');
        if (table) { tables++; } else { cleared++; }
      } else if (table) {
        skippedTables++;
      }
    }
  }

  var out = {updated: updated, missing: missing, walk_ins: walkIns,
             slots: slots, tables: tables, cleared: cleared};
  var notes = [];
  if (skippedWalkIns) {
    notes.push(skippedWalkIns + ' walk-in(s) not recorded: no column named ' +
               'exactly "Walk-in"');
  }
  if (skippedSlots) {
    notes.push(skippedSlots + ' walk-in slot(s) not recorded: no column named ' +
               'exactly "Allocated Time Slot"');
  }
  if (skippedTables) {
    notes.push(skippedTables + ' table number(s) not recorded: add a column ' +
               'headed exactly "Table" to the right of Walk-in');
  }
  if (notes.length) out.note = notes.join('; ');
  return out;
}

function headerList(headers) {
  var out = [];
  for (var c = 0; c < headers.length; c++) {
    var h = String(headers[c]);
    if (h) out.push('[' + (c + 1) + '] ' + h.slice(0, 40));
  }
  return out.join(' | ');
}

function normaliseMatric(v) {
  return String(v == null ? '' : v).toUpperCase().replace(/[^A-Z0-9]/g, '');
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
                       .setMimeType(ContentService.MimeType.JSON);
}

function selfTest() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = RESPONSES_TAB ? ss.getSheetByName(RESPONSES_TAB) : ss.getSheets()[0];
  var values = sheet.getDataRange().getValues();
  var headers = values[0];

  Logger.log('tab       : ' + sheet.getName());
  Logger.log('data rows : ' + Math.max(0, values.length - 1));
  Logger.log('headers   : ' + headerList(headers));

  try {
    var matricCol = findColumn(headers, MATRIC_NAMES, 'the matric number');
    var showupCol = findColumn(headers, SHOWUP_NAMES, 'Show-up');
    var walkinCol = findColumn(headers, WALKIN_NAMES, 'Walk-in');
    var slotCol   = findColumn(headers, SLOT_NAMES, 'Allocated Time Slot');
    var tableCol  = findColumn(headers, TABLE_NAMES, 'Table');
    Logger.log('READ  matric  : ' + (matricCol < 0 ? 'NOT FOUND'
               : 'column ' + (matricCol + 1) + '  "' + headers[matricCol] + '"'));
    Logger.log('WRITE Show-up : ' + (showupCol < 0 ? 'NOT FOUND -- nothing will be written'
               : 'column ' + (showupCol + 1) + '  "' + headers[showupCol] + '"'));
    Logger.log('WRITE Walk-in : ' + (walkinCol < 0 ? 'not present (optional)'
               : 'column ' + (walkinCol + 1) + '  "' + headers[walkinCol] + '"'));
    Logger.log('WRITE Allocated Time Slot (walk-ins only) : ' + (slotCol < 0
               ? 'not present (optional)'
               : 'column ' + (slotCol + 1) + '  "' + headers[slotCol] + '"'));
    Logger.log('WRITE Table (cleared again on undo) : ' + (tableCol < 0
               ? 'not present (optional) -- table numbers will NOT reach the sheet'
               : 'column ' + (tableCol + 1) + '  "' + headers[tableCol] + '"'));
    Logger.log('No other column can be written by this script.');
  } catch (err) {
    Logger.log('PROBLEM: ' + err.message);
  }

  Logger.log(SHARED_SECRET === 'change-me-to-something-random'
             ? 'WARNING: SHARED_SECRET is still the default -- change it, then ' +
               'REDEPLOY (Deploy > Manage deployments > New version). Saving ' +
               'the editor does not change what the /exec URL runs.'
             : 'secret    : set, ' + String(SHARED_SECRET).length + ' characters');
}
