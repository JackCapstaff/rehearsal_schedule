# Timeline Editor - Quick Start Guide

## Feature Quick Reference

### 1️⃣ Snap-to-Grid Dragging
**What:** Drag blocks to move them - they snap to 5-minute marks  
**When:** Rearranging pieces within a rehearsal  
**How:**
```
1. Click and hold a work block
2. Drag left or right
3. Block snaps to nearest 5-min grid line
4. Release to save
5. Auto-saves to server
```

**Visual Cues:**
- Cursor: `grab` (hover) → `grabbing` (dragging)
- Block: Semi-transparent while dragging, enhanced shadow
- Grid: Background shows 5-minute marks

---

### 2️⃣ Drag-to-Swap
**What:** Drag one block over another to swap their positions  
**When:** Reorder pieces without recalculating times  
**How:**
```
1. Click and hold block A (e.g., "Symphony")
2. Drag toward block B (e.g., "Carmen Suite")
3. Block B shows GREEN TOP BORDER
4. Release over the green border
5. Blocks instantly swap positions
6. History shows: "Swapped Symphony and Carmen Suite"
```

**Visual Cues:**
- Green border (3px) appears on target block
- Blocks exchange positions instantly
- History entry added automatically

**Important:** Only works within the **same rehearsal**

---

### 3️⃣ Split View
**What:** View 2+ rehearsals side-by-side to compare scheduling  
**When:** Checking load balance, identifying gaps  
**How:**
```
1. Click "📊 Split View" button at top of timeline
2. Checkboxes appear for each rehearsal
3. Check 2-4 rehearsals you want to compare
4. Rehearsals display side-by-side, scrollable
5. All editing features work normally
6. Click "📋 Exit Split View" to return to normal
```

**Visual Cues:**
- Button text changes: "📊 Split View" ↔ "📋 Exit Split View"
- Rehearsals shown as cards in horizontal row
- Scroll bar at bottom (or arrow keys)

**Editing in Split View:**
- ✅ All drag/snap/swap features work
- ✅ Changes appear in all views
- ✅ Auto-saves work normally

---

## Workflow Examples

### Scenario 1: Quick Reorder
```
Goal: Move "Break" from middle to end of rehearsal

Steps:
1. Locate "Break" block (gray, ~30min chunk)
2. Drag it to the right (toward end of timeline)
3. Snap-to-grid keeps it aligned
4. Release at desired time
5. ✓ Break moved to end, all times auto-adjusted
```

### Scenario 2: Swap Two Pieces
```
Goal: Have "Nutcracker" before "Carmen Suite" (currently reversed)

Steps:
1. Drag "Nutcracker" block
2. Drag over "Carmen Suite" block
3. Green border appears on "Carmen Suite"
4. Release
5. ✓ Pieces swapped, times stay same
6. History: "Swapped Nutcracker and Carmen Suite"
```

### Scenario 3: Balance Across Sessions
```
Goal: Check if three rehearsals are equally balanced

Steps:
1. Click "📊 Split View"
2. Check: Rehearsal 1, Rehearsal 2, Rehearsal 3
3. View all three side-by-side
4. See that Rehearsal 1 is packed (180 min content in 150 min)
5. Drag 30-min "Excerpt" from Rehearsal 1 to Rehearsal 3
6. Verify Rehearsal 3 can fit it (has space)
7. View looks balanced now
8. Click "📋 Exit Split View"
```

---

## Control Panel Reference

### Timeline Editor Top Bar
```
[📊 Split View] button
  ↓
If clicked, expands to:
[📋 Exit Split View]  [Select rehearsals to compare:]
[ ] Rehearsal 1  [ ] Rehearsal 2  [ ] Rehearsal 3
```

### Time Display Format
```
Block Label:  "Symphony No. 5"
Time Range:   "14:30–14:45"     (start – end time)
Duration:     "15 min"          (from duration field)

After dragging to snap at 14:35:
Block updates to: "14:35–14:50"
Duration stays:   "15 min"
```

---

## Tips & Tricks

### ⚡ Quick Tips

1. **Precise positioning**
   - Snap-to-grid prevents off-by-5-minutes errors
   - Blocks always align with rehearsal grid

2. **Undo a swap**
   - Used history panel at bottom of timeline
   - Click the entry before your swap
   - All edits revert to that point

3. **Check total time**
   - All pieces shown with duration badges
   - Add up durations to verify fit in rehearsal

4. **Resize during drag**
   - Can't resize while moving
   - Resize after dropping, or use resize handle

5. **Split view scrolling**
   - Use horizontal scroll bar
   - Or click between cards and use arrow keys
   - Works on desktop and tablet

### 🎯 Best Practices

1. **Plan the order first**
   - Use split view to see all sessions
   - Then make targeted changes

2. **Batch similar changes**
   - All swaps together
   - Then all timing adjustments
   - Reduces history clutter

3. **Use history often**
   - Undo experimental changes
   - Revert to last "good" state
   - No harm in trying things

4. **Export schedule when done**
   - Generate PDF/Excel from timed schedule
   - Share with musicians for practice prep

---

## Troubleshooting

### Issue: Block won't drag
**Solution:** Make sure you're not clicking the resize handle (right edge)

### Issue: Swap didn't work
**Solution:** 
- Swap only works within same rehearsal
- Try dragging the block labeled "Rehearsal 1" only over another "Rehearsal 1" block

### Issue: Split view shows same layout
**Solution:**
- Click the checkbox ✓ next to rehearsal you want to add
- Must check boxes for cards to appear

### Issue: Change didn't save
**Solution:**
- All changes auto-save (no manual save button needed)
- Check browser console for errors (F12)
- Refresh page to sync if unsure

---

## History Panel

### Location
Bottom of timeline editor (click "📋 History" button)

### What It Shows
```
14:32:05  Timeline modified
14:31:42  Swapped Symphony No. 5 and Carmen Suite
14:30:18  Moved Nutcracker to 14:35
```

### How to Use
1. Click any history entry
2. Timeline reverts to that state
3. All future edits after that point are removed

### Permanent Actions
- History is tied to schedule
- Survives page refresh
- Can view old versions (but can't edit after reverting)

---

## Keyboard Shortcuts (Reserved for Future)

| Shortcut | Action |
|----------|--------|
| Arrow Left/Right | Nudge block by 5 min |
| Ctrl+Z | Undo |
| Ctrl+Y | Redo |
| Delete | Remove block |
| Space+Drag | Fine positioning (no snap) |

*Currently these use the UI buttons. Keyboard shortcuts coming in next release.*

---

## Video Walkthrough (Planned)

1. ▶️ "Snap-to-Grid Basics" (2 min)
2. ▶️ "Swapping Pieces" (2 min)
3. ▶️ "Split View Tutorial" (3 min)
4. ▶️ "Full Workflow Example" (5 min)

Check back for video links at main dashboard.

---

## Getting Help

### Common Questions

**Q: Do changes auto-save?**  
A: Yes! Every drag, swap, or resize auto-saves to server. No manual save needed.

**Q: Can I undo?**  
A: Yes! Use the History panel at bottom of timeline. Click any previous state to revert.

**Q: Can I move pieces between rehearsals?**  
A: Current version: Only within same rehearsal (swap/move). Future: Drag between rehearsals.

**Q: Will musicians see this schedule?**  
A: Yes! Publish the schedule and they see the timed rehearsal breakdown.

**Q: Can I export this as PDF?**  
A: Yes! Click "Export PDF" button above timeline (if enabled). Generates rehearsal schedule.

---

## Next Steps

1. **Try snap-to-grid**: Drag any block left/right
2. **Try swap**: Drag one block over another
3. **Try split-view**: Click "Split View" and check 2 rehearsals
4. **Review history**: Click "History" button to see changes
5. **Publish schedule**: When satisfied, publish to share with ensemble

---

*Timeline Editor v2.0 - Enhanced with snap-to-grid, drag-to-swap, and split-view*
