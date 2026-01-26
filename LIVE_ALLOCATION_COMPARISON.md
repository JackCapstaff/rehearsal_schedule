# Live Allocation Comparison Feature

## Overview
The allocation table now shows a **live comparison** of how piece distribution changes as you edit the timeline. This happens **automatically without requiring button clicks** to regenerate allocation/schedule.

## How It Works

### 1. Original Allocation
When you first run "Generate Allocation", the original distribution is saved in `STATE.allocation_original`. This serves as the "before" reference point.

### 2. Current Allocation (Derived)
As you drag and resize pieces in the timeline, the current allocation is **derived on-the-fly** from the timed data via the `deriveAllocationFromTimed()` function. This function:
- Groups timed entries by Title and Rehearsal
- Sums the duration (minutes) for each work in each rehearsal
- Returns allocation in the same format as the original

### 3. Comparison Display
The allocation table shows side-by-side comparison:
- **Format**: `5→10` (original → current)
- **Color Coding**:
  - **Green** (#dcfce7): Minutes increased
  - **Red** (#fee2e2): Minutes decreased  
  - **White**: No change

### 4. Auto-Update on Timeline Edit
Every time you:
- Drag a work block to a different time/rehearsal
- Resize a work to change its duration
- Save changes

The allocation table automatically updates to show how the distribution has changed.

## Technical Implementation

### Functions Added

#### `deriveAllocationFromTimed()`
Located at line 328 in `/static/editor.js`
- Transforms STATE.timed (timeline data) into allocation format
- Groups by Title×Rehearsal, sums durations
- Used to calculate "current" distribution

#### `renderAllocationWithComparison(containerId, originalAlloc, currentAlloc)`
Located at line 388 in `/static/editor.js`
- Renders comparison table with Original→Current format
- Applies color coding based on changes
- Called during initialization and after timeline saves

### Integration Points

1. **Page Load** (initEditor, line 1035):
   ```javascript
   const currentAlloc = deriveAllocationFromTimed();
   renderAllocationWithComparison("allocation-table", STATE.allocation_original, currentAlloc);
   ```

2. **After Timeline Edit** (saveTimelineToBackend, line 1634):
   ```javascript
   const currentAlloc = deriveAllocationFromTimed();
   renderAllocationWithComparison("allocation-table", STATE.allocation_original, currentAlloc);
   ```

## User Workflow

1. **Generate Allocation** (button on page)
   - Allocates works across rehearsals
   - Original allocation stored for comparison

2. **Generate Schedule** (button on page)
   - Orders works within rehearsals
   - Creates timed timeline

3. **Edit Timeline** (drag/resize blocks)
   - Changes reflected in timed data
   - Allocation table updates automatically
   - Shows green (increase) / red (decrease) for changed cells

4. **No Page Reload Required**
   - All updates happen client-side after timeline edits
   - Backend auto-saves the timeline edits (timed data)
   - Allocation comparison recalculated without regenerating

## Files Modified

- `/static/editor.js` (main changes):
  - Added `STATE.allocation_original` field
  - Added `deriveAllocationFromTimed()` function (line 328)
  - Added `renderAllocationWithComparison()` function (line 388)
  - Modified `saveTimelineToBackend()` to update allocation (line 1634)
  - Modified `initEditor()` to use comparison rendering (line 1035)

## Key Benefits

✅ **Live Updates**: See allocation changes instantly as you edit timeline
✅ **No Button Clicks**: Comparison calculated automatically
✅ **Visual Feedback**: Color coding shows increases/decreases at a glance
✅ **No Regeneration**: Uses derived data from timeline, not expensive backend computation
✅ **Data Integrity**: Original allocation always preserved for comparison

## Testing

1. Load a schedule with allocation and timed data
2. In Timeline Editor, drag a work to a different rehearsal
3. Watch the Allocation table update in real-time
4. Check for green/red cells showing the changes
5. Drag the work back
6. Verify the cell colors revert
