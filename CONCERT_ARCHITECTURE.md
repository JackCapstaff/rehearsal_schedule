# Concert Feature Architecture

## 🎯 Overview
The concert system is designed with the **timeline editor as the source of truth**. All concert information flows from Excel imports → Timeline → All Views.

## 📊 Data Flow

```
Excel Import (Event Type = "Concert")
    ↓
Create/Update Concert in ensemble.concerts[]
    ↓
Link concert_id to rehearsal row
    ↓
Concert appears in:
    - Timeline Editor (editable)
    - Schedule View (linked concerts section)
    - Calendar View (🎵 icon)
    - List View
    - Concerts Management Page
    - PDF Export
```

## 🗄️ Data Structure

### Concert Object (stored in `ensemble.concerts[]`)
```json
{
  "id": "concert_a1b2c3d4e5f6",
  "title": "Rotherham Symphony Orchestra - Concert - 2026-02-15",
  "date": "2026-02-15",
  "time": "13:00:00",
  "venue": "Town Hall",
  "uniform": "Black tie",
  "programme": "Beethoven Symphony No. 5\nMozart Piano Concerto No. 21",
  "other_info": "Doors open 30 minutes before",
  "status": "scheduled",
  "schedule_id": "sched_abc123",
  "is_auto_generated": true,
  "created_at": "2026-01-27T10:00:00Z"
}
```

### Rehearsal Row (with Concert link)
```json
{
  "Rehearsal": 6,
  "Date": "2026-02-15",
  "Start Time": "13:00:00",
  "End Time": "16:00:00",
  "Event Type": "Concert",
  "Section": "Full Ensemble",
  "Include in allocation": "N",
  "concert_id": "concert_a1b2c3d4e5f6"  // ← Links to concert
}
```

### Timed Row (generated)
```json
{
  "Rehearsal": 6,
  "Title": "Concert",
  "Time in Rehearsal": "13:00",
  "Date": "2026-02-15",
  "Event Type": "Concert",
  "concert_id": "concert_a1b2c3d4e5f6"  // ← Linked dynamically
}
```

## 🔄 Key Operations

### 1. Excel Import Creates/Updates Concerts
**File**: `app.py` lines 3170-3222

When importing Excel with `Event Type = "Concert"`:
1. Check if concert exists for this schedule + date
2. If exists: **Update** fields from Excel
3. If new: **Create** concert with unique ID
4. Link `concert_id` back to rehearsal row
5. Save to `ensemble.concerts[]`

**Excel Columns Read**:
- `Concert Title` → concert.title (or auto-generated)
- `Venue` → concert.venue
- `Uniform` → concert.uniform
- `Programme` → concert.programme
- `Other Info` → concert.other_info
- `Start Time` → concert.time
- `Date` → concert.date

### 2. Timeline Editor Links concert_id to Timed Data
**File**: `app.py` lines 1500-1540 (api_schedule_data)

When returning timed data:
1. For each timed row, find matching rehearsal
2. If rehearsal Event Type = "Concert", find concert by date
3. Add `concert_id` to timed row
4. Frontend can now access concert for editing

### 3. Editing Concerts Updates Master Record
**Endpoints**:
- `PUT /api/s/<schedule_id>/concert/<concert_id>` - Timeline editor
- `PUT /api/ensembles/<ensemble_id>/concerts/<concert_id>` - Concerts page

Both call `update_concert()` which updates `ensemble.concerts[]`

### 4. All Views Pull from Master Concert Record

| View | Access Path | Display |
|------|-------------|---------|
| **Timeline Editor** | STATE.timed[].concert_id → Edit modal | Full edit capability |
| **Schedule View** | ensemble.concerts[] filtered by schedule_id | Linked Concerts section |
| **Calendar View** | member_dashboard concerts passed from backend | 🎵 Concert blocks |
| **List View** | Same as calendar | "Ensemble - Concert - Date" |
| **Concerts Page** | Direct from ensemble.concerts[] | Management table |
| **PDF Export** | Via schedule view data | Full details |

## 🎨 User Experience Flow

### Creating a Concert
1. **Excel Import**: Add row with `Event Type = "Concert"`
2. **Fill Fields**: Concert Title, Venue, Uniform, Programme, etc.
3. **Import**: Concert auto-creates with unique ID
4. **Appears Everywhere**: Timeline, schedule, calendar, concerts page

### Editing a Concert
**Option A - Timeline Editor**:
1. Double-click concert block OR right-click → Edit Event
2. Modal opens with all fields pre-populated
3. Edit title, venue, uniform, programme, etc.
4. Save → Updates master concert record

**Option B - Schedule View**:
1. Scroll to "Linked Concerts" section
2. Click "✎ Edit Concert" button
3. Same modal as timeline editor
4. Save → Updates master record

**Option C - Concerts Management Page**:
1. Go to `/admin/ensembles/<id>/concerts`
2. Find concert in list
3. Click edit button
4. Form with all fields
5. Save → Updates master record

### Viewing Concerts
- **Calendar**: Shows as 🎵 green blocks on dates
- **List**: "Ensemble - Concert - Date - Time - Venue"
- **Schedule**: Full details with programme, uniform, other info
- **Timeline**: Concert blocks (cyan/turquoise color)

## 🔧 Technical Details

### Deduplication
- Concerts are deduplicated by `schedule_id` + `date`
- Successive Excel imports **update** existing concerts (no duplicates)
- Old `auto_concert_*` duplicates can be cleaned via admin maintenance

### Concert ID Generation
- Format: `concert_[12 hex chars]`
- Example: `concert_a1b2c3d4e5f6`
- Unique, persistent across imports

### Title Auto-Generation
- Format: `[Ensemble Name] - Concert - [Date]`
- Example: "Rotherham Symphony Orchestra - Concert - 2026-02-15"
- Can be overridden via "Concert Title" column in Excel

### Status Field
- `scheduled`: Active, visible everywhere
- `completed`: Past concerts
- `cancelled`: Cancelled events
- Only "scheduled" concerts display in calendar/views

## 🧹 Maintenance

### Clean Up Duplicate Concerts
**Admin Home** → **System Maintenance** → **🧹 Clean Up Duplicate Concerts**

Removes old `auto_concert_*` duplicates from before deduplication fix.

### Concert Validation
All concerts must have:
- Valid `date` (YYYY-MM-DD)
- Valid `schedule_id` (links to schedule)
- Valid `ensemble_id` (via parent ensemble)
- `status = "scheduled"` to be visible

## 🚀 Future Enhancements

1. **Ticket Sales Integration**: Link to ticketing systems
2. **Programme Builder**: Drag-drop works into concert programme
3. **Rehearsal → Concert Mapping**: Show which works will be performed
4. **Concert Series**: Group related concerts
5. **Export Options**: iCal, Google Calendar sync
6. **Public Concert Pages**: Shareable concert details

## 📝 Developer Notes

### Adding New Concert Fields
1. Update `Concert Object` structure above
2. Add field to Excel import (line ~3188)
3. Update `update_concert()` function (line ~585)
4. Add to edit modals (static/editor.js, templates/view.html)
5. Update display in all views

### Testing Checklist
- [ ] Import Excel with Concert event type
- [ ] Verify concert appears in Concerts page
- [ ] Check concert shows in Schedule View
- [ ] Confirm calendar displays concert
- [ ] Double-click concert in timeline → modal opens
- [ ] Edit concert details → save → changes persist
- [ ] Re-import same Excel → concert updates (no duplicate)
- [ ] PDF export includes concert details

## 📞 Support
For issues or questions, see main README.md
