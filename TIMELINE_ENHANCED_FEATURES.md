# Timeline Editor - Enhanced Features

## Feature 1: Snap-to-Grid Dragging

### How It Works
When you drag a work block horizontally, it **snaps to the nearest 5-minute boundary** automatically.

```
Without Snap: Block can be at any pixel position (messy)
With Snap:    Block snaps to 5-min grid (aligned)

Example:
- Drag block to 57px → Snaps to 50px (5-min mark)
- Drag block to 152px → Snaps to 150px (exact 5-min mark)
```

### Pixel-to-Time Conversion
```javascript
// Each grid snap point = exactly one 5-minute interval
pixelsPerBlock = 100
timePerBlock = 5 minutes

// Position math
position (minutes) = (positionInPixels / 100) * 5
```

### Visual Feedback
- Blocks **highlight and enlarge shadow** while dragging
- Shows **exact 5-min alignment** via grid background
- Cursor changes to **"grabbing"** hand
- Block snaps smoothly as you drag

### Use Case
Perfect for **quick scheduling adjustments** where pieces should always align with 5-minute intervals (rehearsal planning common practice).

---

## Feature 2: Drag-to-Swap

### How It Works
Drag one work block **over another** to swap their positions in the rehearsal.

### Step-by-Step
1. Click and hold on a work block (e.g., "Symphony No. 5")
2. Drag over another block (e.g., "Carmen Suite")
3. **Green border appears** on the target block → indicates swap target
4. Release the mouse
5. Blocks **exchange positions** instantly
6. Changes auto-saved with history entry

### Example Scenario
```
Before Swap:
Rehearsal 1
├─ [Symphony No. 5]  14:00–14:15
├─ [Carmen Suite]    14:15–14:25
└─ [Nutcracker]      14:25–14:40

After Dragging Symphony onto Carmen:
Rehearsal 1
├─ [Carmen Suite]    14:00–14:10
├─ [Symphony No. 5]  14:10–14:25
└─ [Nutcracker]      14:25–14:40
```

### Constraints
- **Only works within same rehearsal** (can't drag between rehearsals)
- Swap **preserves all timing data** (start times, durations)
- **Entire rows swap** (no partial swaps)

### History Integration
Every swap is logged with description:
```
"Swapped Symphony No. 5 and Carmen Suite"
```

Can be reverted using the History panel.

### Use Case
**Reorder pieces without recalculating times** - useful when conductor wants to change arrangement mid-planning.

---

## Feature 3: Split View

### How It Works
Compare **multiple rehearsals side-by-side** to see scheduling across sessions.

### Activation
1. Click **"📊 Split View"** button in timeline controls
2. Checkboxes appear for each rehearsal
3. **Check rehearsals to display** side-by-side
4. Click **"📋 Exit Split View"** to return to normal

### Layout
```
Normal View:                Split View:
┌───────────────┐          ┌──────────┐  ┌──────────┐  ┌──────────┐
│ Rehearsal 1   │          │Rehearsal│  │Rehearsal│  │Rehearsal│
│ [blocks...]   │          │   1     │  │   2     │  │   3     │
├───────────────┤          │[blocks]│  │[blocks]│  │[blocks]│
│ Rehearsal 2   │    →     │        │  │        │  │        │
│ [blocks...]   │          └──────────┘  └──────────┘  └──────────┘
├───────────────┤              ← Scroll left/right →
│ Rehearsal 3   │
│ [blocks...]   │
└───────────────┘
```

### Features in Split View
- Each rehearsal shown as **side-by-side card**
- Can **scroll horizontally** to see all selected rehearsals
- **All drag/swap/resize features still work** in split view
- Each card can be **individually edited**
- Changes sync across all views

### Comparison Scenarios

#### Scenario 1: Check Distribution Across Sessions
```
Split View shows:
- Rehearsal 1: Total 180 min (very packed)
- Rehearsal 2: Total 120 min (light)
- Rehearsal 3: Total 150 min (medium)

→ Can rebalance by dragging pieces between rehearsals
```

#### Scenario 2: Ensure Consistency
```
Verifying piece placement:
- See if "Symphony No. 5" appears in multiple rehearsals at similar times
- Ensures musicians know when to expect each piece
```

#### Scenario 3: Identify Gaps
```
Visual scan for:
- Rehearsals with too many breaks
- Sessions where pieces don't fit well
- Time-allocation imbalances
```

### Scroll Behavior
- **Horizontal scrolling only** - resize browser or use arrow keys
- Each card maintains independent scroll state
- Minimizes card width to 400px for compact display

### Editing in Split View
All editing operations work normally:
- ✅ Drag blocks to move (snaps to grid)
- ✅ Drag over blocks to swap
- ✅ Drag resize handles to change duration
- ✅ All changes auto-save
- ✅ History tracks all edits

### Exit Split View
1. Click **"📋 Exit Split View"** button
2. Returns to normal **stacked rehearsal view**
3. All changes preserved

### Use Case
**High-level rehearsal planning** - see entire schedule at a glance, identify bottlenecks, balance workload across sessions.

---

## Combined Workflow Example

### Scenario: Rebalance Over-Scheduled Rehearsal

1. **Activate Split View** → See all rehearsals side-by-side
2. **Identify problem**: Rehearsal 2 is packed (240 min of content in 180-min session)
3. **Select piece to move**: "Nutcracker" (40 min) in Rehearsal 2
4. **Drag with snap-to-grid**: Move it to start at 14:00 in Rehearsal 3
5. **Check alignment**: Verify it doesn't overlap with existing pieces
6. **Use swap if needed**: Drag one piece over another to reorganize
7. **Review in split view**: Confirm both rehearsals now balanced
8. **Exit split view**: Return to normal view, verify total times
9. **Check history**: See all moves/swaps tracked

### Result
```
Before:
- Rehearsal 2: 240 min content / 180 min available = OVER
- Rehearsal 3: 100 min content / 180 min available = UNDER

After:
- Rehearsal 2: 200 min content / 180 min available = Trim 20 min
- Rehearsal 3: 140 min content / 180 min available = Balanced ✓
```

---

## Technical Details

### Snap-to-Grid Implementation
```javascript
// Snap calculation during drag:
snappedDelta = Math.round(mouseDelta / 100) * 100  // 100 = pixelsPerBlock
newPosition = Math.max(0, originalPosition + snappedDelta)

// Result: Blocks can only be at 0px, 100px, 200px, etc.
//         Which corresponds to 0:00, 0:05, 0:10, etc.
```

### Swap Detection
```javascript
// When block dropped over another:
dragIndex = parseInt(draggedBlock.dataset.origIndex)
dropIndex = parseInt(targetBlock.dataset.origIndex)

// Swap in data:
[STATE.timed[dragIndex], STATE.timed[dropIndex]] = 
[STATE.timed[dropIndex], STATE.timed[dragIndex]]

// Re-render and save
```

### Split View Persistence
```javascript
// Selected rehearsals stored in:
TIMELINE_STATE.splitViewEnabled  // true/false
// Checkboxes contain selections (not persisted between sessions)

// All edits in split view go to same STATE.timed
// Changes saved normally via saveTimelineEdits()
```

---

## Browser Support
- ✅ Chrome 90+
- ✅ Firefox 88+
- ✅ Safari 14+
- ✅ Edge 90+

All features use standard APIs (no experimental features).

---

## Keyboard Shortcuts (Future)
Potential enhancements:
- `Arrow Left/Right` - Nudge selected block by 5 min
- `Ctrl+Z` - Undo (integrated with history)
- `Ctrl+Shift+S` - Toggle split view
- `Delete` - Remove block (with confirmation)

Currently implemented via UI buttons/drag only.
