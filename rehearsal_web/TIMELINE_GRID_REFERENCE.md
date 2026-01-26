# Timeline Grid System - Quick Reference

## Grid Dimensions

```
One Block = 5 minutes
1 Block Width = 100 pixels

Examples:
- 5 min piece → 1 block → 100px wide
- 10 min piece → 2 blocks → 200px wide
- 15 min piece → 3 blocks → 300px wide
- 60 min piece → 12 blocks → 1200px wide
```

## Visual Layout Example

```
┌─────────────────────────────────────────────────────────────┐
│ Rehearsal 1                                                 │
├─────────────────────────────────────────────────────────────┤
│ [Symphony No. 5]  [Carmen Suite]  [Nutcracker]  [Break]   │
│ 
│ Grid:  |-------|-------|-------|-------|-------|-------|
│        0:00    5:00   10:00   15:00   20:00   25:00   30:00
│
│ Each vertical line = 5 minutes
└─────────────────────────────────────────────────────────────┘
```

## Block Positioning Formula

```javascript
// Start time in rehearsal (e.g., "14:30")
startMinutes = 14 * 60 + 30 = 870 minutes

// Convert to block offset
leftPixels = (startMinutes / 5) * 100 = 17400px

// Duration in minutes (e.g., 15 min piece)
blockCount = Math.round(15 / 5) = 3

// Width in pixels
widthPixels = 3 * 100 = 300px
```

## User Interactions

### Moving a Block (Drag Center)
```
1. Click and hold on work block
2. Mouse cursor changes to "grab"
3. Drag left/right to reposition
4. Block moves in real-time
5. Release to finalize
6. Auto-saves to server
```

### Resizing a Block (Drag Right Edge)
```
1. Position cursor on right edge (resize handle appears)
2. Mouse cursor changes to "col-resize"
3. Drag right to expand (adds 5 min per block)
4. Drag left to shrink (removes 5 min per block)
5. Block width changes in real-time
6. Release to finalize
7. Auto-saves to server
```

## Data Persistence

```javascript
// Each work item stores:
{
  "Title": "Symphony No. 5",
  "Time in Rehearsal": "14:30",           // Changed by drag (move)
  "Rehearsal Time (minutes)": 15,         // Changed by resize
  "Rehearsal": 1                          // Which rehearsal session
}
```

## Color Scheme

```
State               | Background  | Border         | Cursor
─────────────────────────────────────────────────────────
Normal              | #3b82f6     | #1e40af        | grab
Hover               | #2563eb     | #1e40af        | grab
Dragging            | #3b82f6 60% | #1e40af        | grabbing
Resizing            | #3b82f6     | #fbbf24 glow   | col-resize
Resize Handle       | transparent | #1e40af        | col-resize
Grid Lines          | #e5e7eb     | (lines only)   | default
```

## Multi-Rehearsal Support

```
┌─────────────────────────────────────────────────┐
│ Rehearsal 1 (180 min total)                    │
├──────────────────────────────────────────────────┤
│ [Block 1] [Block 2] [Block 3]   [Grid lines]  │
├──────────────────────────────────────────────────┤
│ Rehearsal 2 (240 min total - different duration)│
├────────────────────────────────────────────────────┤
│ [Block 1] [Block 2] [Block 3] [Block 4] [Grid]  │
└────────────────────────────────────────────────────┘

Each rehearsal's grid width = (rehearsalEndTime - startTime - breaks)
                               ÷ 5 min × 100px
```

## Edge Cases Handled

1. **Different rehearsal durations**: Each gets proportional grid width
2. **Breaks**: Skipped in rendering (no blocks, just empty time)
3. **Overlapping times**: Visual (blocks can stack if data allows)
4. **Very long pieces**: Blocks extend multiple grid widths
5. **Very short pieces**: Minimum 1 block (5 min)

## Auto-Save Behavior

```
User Action → Real-time Visual Update → mouseup/drag end
                                             ↓
                                      saveTimelineEdits()
                                             ↓
                                      PUT /api/s/{id}/timed_edit
                                             ↓
                                      Server saves + history entry
                                             ↓
                                      Browser displays "✓ Saved"
```

## History Integration

All timeline edits logged:
```json
{
  "action": "edit",
  "description": "Timeline modified",
  "timestamp": 1703082000,
  "timed": [ /* full timed array */ ]
}
```

Users can revert using "History" panel at bottom of timeline.
