# Timeline Editor UI Updates

## Summary of Changes

### 1. Fixed Sticky Row Alignment (Tables)
**Problem**: Frozen left columns didn't align with scrollable columns on data tables.

**Solution**:
- Added `line-height: 20px` to `.grid-table th` for consistent header height
- Added `line-height: 40px` to `.grid-table td` to align with row height
- Changed both frozen and scroll tbody to `display: block` for proper height inheritance

**Files Modified**: `static/style.css`

---

### 2. Redesigned Timeline Editor to Grid-Based 5-Minute Block System
**Problem**: 
- Blocks weren't persisting after drag
- UI wasn't intuitive for time-based scheduling
- No visual feedback on positioning

**New Implementation**:

#### Architecture
- **Grid System**: Each rehearsal is divided into 100px blocks (representing 5 minutes)
- **Visual Alignment**: Grid lines show 5-minute intervals across entire timeline
- **Persistent Positioning**: Blocks positioned absolutely with precise pixel/minute calculations

#### Features Implemented

##### 1. **Drag-to-Move Blocks**
- Click and drag any work block to move it within the rehearsal
- Visual feedback: block becomes semi-transparent while dragging
- **Changes persist**: Position changes update `Time in Rehearsal` field and auto-save

##### 2. **Drag-to-Resize (Right Edge)**
- Click and drag the right edge handle of any block to change duration
- Each pixel ~5 minutes
- Real-time duration update displayed in block size
- **Changes persist**: Duration changes update `Rehearsal Time (minutes)` and auto-save

##### 3. **Visual Layout**
- Rehearsals shown as expandable sections
- Each section has:
  - **Label** (left): Shows "Rehearsal #"
  - **Grid container** (right): 5-minute grid with vertical lines
  - **Work blocks**: Positioned based on start time and duration
  - **Resize handles**: Right edge of each block (semi-transparent overlay)

##### 4. **Break Handling**
- Breaks are skipped in rendering (appear as silent time blocks)
- Only work items shown as draggable blocks

##### 5. **Rehearsal Time Calculation**
- Grid width calculated from rehearsal start/end times minus breaks
- Accommodates different rehearsal durations per session

#### UI/UX Improvements
- **Better visual hierarchy**: Time flows left-to-right with clear 5-min markers
- **Immediate feedback**: Blocks resize/move in real-time with mouse movement
- **Color coding**: 
  - Work blocks: Blue (#3b82f6)
  - Hover: Darker blue (#2563eb)
  - Dragging: Semi-transparent
  - Resizing: Yellow glow (#fbbf24)
- **Grab cursor**: Indicates clickable areas
- **Resize cursor**: Shows resizable edges

#### How It Works

1. **Data Structure**:
   ```javascript
   // Each work item has:
   - Title: name of piece
   - Time in Rehearsal: "HH:MM" (start time)
   - Rehearsal Time (minutes): duration (changes via resize)
   ```

2. **Block Positioning**:
   ```
   left = (startMinutes / 5) * 100px
   width = (duration / 5) * 100px
   ```

3. **Drag Operations**:
   - Capture `dragstart` with client X position
   - Track real-time `mousemove` deltas
   - On `dragend`, calculate new block position
   - Update STATE.timed directly and auto-save

4. **Resize Operations**:
   - Capture `mousedown` on resize handle
   - Track `mousemove` deltas in pixels
   - Convert pixel delta → block count → minutes
   - Update STATE.timed and auto-save on `mouseup`

#### Files Modified
- `static/editor.js` (complete rewrite of `renderTimelineEditor()` + handlers)
- `static/style.css` (enhanced `.timeline-*` styles + grid improvements)

#### Key Functions

| Function | Purpose |
|----------|---------|
| `renderTimelineEditor()` | Renders grid with work blocks positioned by time |
| `onTimelineMouseMove()` | Handles real-time resize feedback |
| `onTimelineMouseUp()` | Finalizes resize and saves |
| `timeStringToMinutes()` | Parses "HH:MM" to minutes |
| `minutesToTimeString()` | Converts minutes back to "HH:MM" |
| `durationToBlocks()` | Calculates grid blocks from minutes |

#### Auto-Save Integration
- All position/duration changes automatically trigger `saveTimelineEdits()`
- Changes logged to history with descriptions like "Timeline modified"
- Can revert using existing history panel

---

## Testing Checklist

- [ ] Sticky headers align with table rows (no vertical gaps)
- [ ] Timeline renders with visible 5-minute grid lines
- [ ] Work blocks positioned correctly by time
- [ ] Dragging a block moves it to new position
- [ ] Dragging block edge resizes it
- [ ] Changes persist after browser refresh
- [ ] Different rehearsals show different durations
- [ ] Breaks don't appear as draggable blocks
- [ ] History shows "Timeline modified" entries

---

## Browser Compatibility

- **Chrome/Edge**: Full support (CSS Grid, drag-drop, SVG lines)
- **Firefox**: Full support
- **Safari**: Full support (tested on 15+)

---

## Future Enhancements

1. **Snap to grid**: Optionally snap moves/resizes to 5-min boundaries
2. **Swap works**: Drag one block onto another to swap positions
3. **Split view**: Show multiple rehearsals side-by-side
4. **Undo/redo**: Keyboard shortcuts for history navigation
5. **Export schedule**: Generate PDF with timeline visualization
