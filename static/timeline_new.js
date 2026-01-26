// ========================
// NEW TIMELINE EDITOR 
// ========================

/**
 * Render timeline editor with drag-to-move and resize functionality
 */
function renderTimelineEditor() {
  const contentEl = document.getElementById("timelineContent");
  if (!contentEl) {
    console.warn("Timeline element #timelineContent not found");
    return;
  }

  // Add undo/redo/revert buttons if not already present
  if (!document.getElementById("timelineUndoBtn")) {
    const controls = document.createElement("div");
    controls.className = "timeline-controls";
    controls.style.cssText = "display:flex; gap:8px; margin-bottom:12px;";
    controls.innerHTML = `
      <button id="timelineUndoBtn" onclick="timelineUndo()" disabled class="btn secondary">
        ↶ Undo
      </button>
      <button id="timelineRedoBtn" onclick="timelineRedo()" disabled class="btn secondary">
        ↷ Redo
      </button>
      <button id="timelineRevertBtn" onclick="revertToOriginal()" disabled class="btn secondary" style="margin-left:auto;">
        ⟲ Revert to Original
      </button>
    `;
    contentEl.parentElement.insertBefore(controls, contentEl);
  }

  try {
    if (!STATE.timed || STATE.timed.length === 0) {
      contentEl.innerHTML = '<div style="padding:20px; color:#999;">No timed schedule yet. Run allocation and generate schedule first.</div>';
      return;
    }

    // Group by rehearsal
    const byRehearsal = {};
    STATE.timed.forEach((row, idx) => {
      const rnum = parseInt(row.Rehearsal);
      if (!byRehearsal[rnum]) byRehearsal[rnum] = [];
      byRehearsal[rnum].push({ ...row, _index: idx });
    });

    // Create grid container
    const gridDiv = document.createElement("div");
    gridDiv.className = "timeline-grid";
    gridDiv.style.cssText = "display:flex; gap:20px; overflow-x:auto; padding:12px; background:#fafafa; border:1px solid #e5e7eb; border-radius:4px;";

    // Render each rehearsal
    Object.keys(byRehearsal)
      .sort((a, b) => parseInt(a) - parseInt(b))
      .forEach(rnum => {
        const column = renderRehearsalColumn(parseInt(rnum), byRehearsal[rnum]);
        gridDiv.appendChild(column);
      });

    contentEl.innerHTML = "";
    contentEl.appendChild(gridDiv);

    // Update history buttons
    updateHistoryButtons();

  } catch (err) {
    console.error("Error rendering timeline:", err);
    contentEl.innerHTML = '<div style="padding:20px; color:red;">Error: ' + escapeHtml(err.message) + '</div>';
  }
}

/**
 * Render a single rehearsal column
 */
function renderRehearsalColumn(rehearsalNum, works) {
  const bounds = getRehearsalBounds(rehearsalNum);
  if (!bounds) {
    const errDiv = document.createElement("div");
    errDiv.textContent = `Rehearsal ${rehearsalNum} not found`;
    return errDiv;
  }

  // Convert to timeline items
  const items = timedToTimelineItems(STATE.timed, rehearsalNum);

  // Column container
  const column = document.createElement("div");
  column.style.cssText = "flex: 0 0 350px; border:1px solid #d1d5db; border-radius:4px; overflow:hidden; background:white; display:flex; flex-direction:column;";

  // Header
  const header = document.createElement("div");
  header.style.cssText = "padding:12px; background:#f3f4f6; border-bottom:1px solid #d1d5db; font-weight:600; text-align:center;";
  header.textContent = `Rehearsal ${rehearsalNum}`;
  column.appendChild(header);

  // Timeline container (scrollable)
  const scrollContainer = document.createElement("div");
  scrollContainer.style.cssText = "flex: 1; overflow-y:auto; overflow-x:hidden; position:relative;";

  // Calculate height based on rehearsal duration
  const durationMins = bounds.totalMins;
  const containerHeight = durationMins * TIMELINE_CONFIG.pixelsPerMinute + 40;

  // Timeline content (with grid lines and labels)
  const timelineContent = document.createElement("div");
  timelineContent.style.cssText = `position:relative; width:100%; height:${containerHeight}px; background:white; padding-left:60px;`;
  timelineContent.dataset.rehearsalNum = rehearsalNum;

  // Add grid lines and time labels
  const numLines = Math.ceil(durationMins / TIMELINE_CONFIG.G) + 1;
  for (let i = 0; i <= numLines; i++) {
    const mins = i * TIMELINE_CONFIG.G;
    const y = mins * TIMELINE_CONFIG.pixelsPerMinute;
    
    // Grid line
    const line = document.createElement("div");
    line.style.cssText = `position:absolute; left:0; right:0; top:${y}px; height:1px; background:${i === 0 ? '#999' : '#e5e7eb'}; z-index:0;`;
    timelineContent.appendChild(line);

    // Time label (every 10 minutes)
    if (i % 2 === 0) {
      const label = document.createElement("div");
      const absTime = addMinutes(bounds.startTime, mins);
      label.textContent = absTime.substring(0, 5); // HH:MM
      label.style.cssText = `position:absolute; left:5px; top:${y - 10}px; font-size:11px; color:#666; width:50px; text-align:right;`;
      timelineContent.appendChild(label);
    }
  }

  // Render timeline items
  items.forEach((item, idx) => {
    const itemElement = renderTimelineItem(item, rehearsalNum, bounds);
    timelineContent.appendChild(itemElement);
  });

  scrollContainer.appendChild(timelineContent);
  column.appendChild(scrollContainer);

  return column;
}

/**
 * Render a single timeline item (work or break)
 */
function renderTimelineItem(item, rehearsalNum, bounds) {
  const startMins = timeStringToMinutes(item.start_time) - bounds.startMins;
  const topPx = startMins * TIMELINE_CONFIG.pixelsPerMinute;
  const heightPx = item.duration_mins * TIMELINE_CONFIG.pixelsPerMinute;

  const endTime = addMinutes(item.start_time, item.duration_mins);

  // Main item container
  const itemEl = document.createElement("div");
  itemEl.className = "timeline-item";
  itemEl.id = item.id;
  itemEl.dataset.itemId = item.id;
  itemEl.dataset.rehearsalNum = rehearsalNum;
  itemEl.dataset.originalIndex = item.originalIndex;

  const bgColor = item.is_break ? '#f59e0b' : '#3b82f6';
  const borderColor = item.is_break ? '#d97706' : '#2563eb';

  itemEl.style.cssText = `
    position:absolute;
    left:0;
    right:0;
    top:${topPx}px;
    height:${heightPx}px;
    background:${bgColor};
    border:1px solid ${borderColor};
    border-radius:4px;
    color:white;
    cursor:grab;
    user-select:none;
    z-index:10;
    margin:2px;
    display:flex;
    flex-direction:column;
    transition: filter 0.2s, opacity 0.2s;
  `;

  // Top resize handle
  const resizeTop = document.createElement("div");
  resizeTop.className = "resize-handle resize-top";
  resizeTop.style.cssText = `
    position:absolute;
    top:0;
    left:0;
    right:0;
    height:${TIMELINE_CONFIG.resizeHandleHeight}px;
    background:rgba(255,255,255,0.2);
    cursor:ns-resize;
    border-radius:4px 4px 0 0;
    z-index:20;
  `;
  resizeTop.addEventListener("mouseenter", () => resizeTop.style.background = "rgba(255,255,255,0.4)");
  resizeTop.addEventListener("mouseleave", () => resizeTop.style.background = "rgba(255,255,255,0.2)");
  itemEl.appendChild(resizeTop);

  // Content (middle - move zone)
  const content = document.createElement("div");
  content.className = "item-content";
  content.style.cssText = `
    flex:1;
    padding:8px 12px;
    overflow:hidden;
    display:flex;
    flex-direction:column;
    justify-content:center;
    cursor:grab;
  `;
  content.innerHTML = `
    <div style="font-weight:600; font-size:12px; margin-bottom:4px;">${escapeHtml(item.title)}</div>
    <div style="font-size:11px; opacity:0.9;">${item.start_time.substring(0,5)}–${endTime.substring(0,5)}</div>
    <div style="font-size:10px; opacity:0.8;">${item.duration_mins} min</div>
  `;
  itemEl.appendChild(content);

  // Bottom resize handle
  const resizeBottom = document.createElement("div");
  resizeBottom.className = "resize-handle resize-bottom";
  resizeBottom.style.cssText = `
    position:absolute;
    bottom:0;
    left:0;
    right:0;
    height:${TIMELINE_CONFIG.resizeHandleHeight}px;
    background:rgba(255,255,255,0.2);
    cursor:ns-resize;
    border-radius:0 0 4px 4px;
    z-index:20;
  `;
  resizeBottom.addEventListener("mouseenter", () => resizeBottom.style.background = "rgba(255,255,255,0.4)");
  resizeBottom.addEventListener("mouseleave", () => resizeBottom.style.background = "rgba(255,255,255,0.2)");
  itemEl.appendChild(resizeBottom);

  // Hover effect on content
  content.addEventListener("mouseenter", () => itemEl.style.filter = "brightness(1.15)");
  content.addEventListener("mouseleave", () => {
    if (!TIMELINE_STATE.isDragging) itemEl.style.filter = "brightness(1)";
  });

  // Attach event handlers
  attachItemEventHandlers(itemEl, item, rehearsalNum, content, resizeTop, resizeBottom);

  return itemEl;
}

/**
 * Attach drag and resize event handlers to timeline item
 */
function attachItemEventHandlers(itemEl, item, rehearsalNum, content, resizeTop, resizeBottom) {
  let startY = 0;
  let startTop = 0;
  let startDuration = 0;

  // MOVE: MouseDown on content
  const onMoveStart = (e) => {
    e.preventDefault();
    e.stopPropagation();

    TIMELINE_STATE.isDragging = true;
    TIMELINE_STATE.draggingItemId = item.id;
    TIMELINE_STATE.dragZone = "move";
    TIMELINE_STATE.dragStartY = e.clientY;

    startY = e.clientY;
    startTop = parseFloat(itemEl.style.top);

    itemEl.style.opacity = "0.7";
    itemEl.style.cursor = "grabbing";
    content.style.cursor = "grabbing";
    itemEl.style.zIndex = "1000";

    // Store original state for history
    if (TIMELINE_HISTORY.stack.length === 0 || TIMELINE_HISTORY.currentIndex === -1) {
      // First edit - save original state
      const originalState = deepCopy(STATE.timed);
      TIMELINE_HISTORY.stack.push({
        timestamp: Date.now(),
        operation: "initial",
        before: originalState,
        after: originalState,
      });
      TIMELINE_HISTORY.currentIndex = 0;
    }

    document.addEventListener("mousemove", onMoveMove);
    document.addEventListener("mouseup", onMoveEnd);
  };

  const onMoveMove = (e) => {
    const deltaY = e.clientY - startY;
    const newTop = Math.max(0, startTop + deltaY);

    // Snap to 5-minute increments
    const snappedTop = Math.round(newTop / (TIMELINE_CONFIG.G * TIMELINE_CONFIG.pixelsPerMinute)) * (TIMELINE_CONFIG.G * TIMELINE_CONFIG.pixelsPerMinute);

    itemEl.style.top = snappedTop + "px";

    // Calculate target time
    const bounds = getRehearsalBounds(rehearsalNum);
    const offsetMins = snappedTop / TIMELINE_CONFIG.pixelsPerMinute;
    const targetTime = addMinutes(bounds.startTime, offsetMins);

    // Preview the move
    const timeline = timedToTimelineItems(STATE.timed, rehearsalNum);
    const draggedItem = timeline.find(i => i.id === item.id);

    if (draggedItem) {
      const result = calculatePushResult(draggedItem, targetTime, timeline, rehearsalNum);

      if (result.valid) {
        itemEl.style.border = "2px solid #22c55e";
        TIMELINE_STATE.previewValid = true;
        TIMELINE_STATE.previewTimeline = result.timeline;
      } else {
        itemEl.style.border = "2px solid #ef4444";
        TIMELINE_STATE.previewValid = false;
        console.log("Invalid move:", result.errors);
      }
    }
  };

  const onMoveEnd = (e) => {
    document.removeEventListener("mousemove", onMoveMove);
    document.removeEventListener("mouseup", onMoveEnd);

    itemEl.style.opacity = "1";
    itemEl.style.cursor = "grab";
    content.style.cursor = "grab";
    itemEl.style.zIndex = "10";
    itemEl.style.border = "";

    if (TIMELINE_STATE.previewValid && TIMELINE_STATE.previewTimeline) {
      // Apply the move
      const beforeState = deepCopy(STATE.timed);

      // Update STATE.timed with new timeline
      const updatedItems = timelineItemsToTimed(TIMELINE_STATE.previewTimeline);
      updatedItems.forEach(updated => {
        const idx = STATE.timed.findIndex(t => t._index === updated._index);
        if (idx !== -1) {
          STATE.timed[idx]['Time in Rehearsal'] = updated['Time in Rehearsal'];
          STATE.timed[idx]['Rehearsal Time (minutes)'] = updated['Rehearsal Time (minutes)'];
        }
      });

      const afterState = deepCopy(STATE.timed);

      // Push to history
      pushToHistory("move", beforeState, afterState);

      // Save and re-render
      saveTimelineToBackend();
      renderTimelineEditor();
    } else {
      // Snap back to original position
      itemEl.style.top = startTop + "px";
      itemEl.style.animation = "shake 0.3s";
      setTimeout(() => itemEl.style.animation = "", 300);
    }

    TIMELINE_STATE.isDragging = false;
    TIMELINE_STATE.draggingItemId = null;
    TIMELINE_STATE.previewValid = false;
    TIMELINE_STATE.previewTimeline = null;
  };

  content.addEventListener("mousedown", onMoveStart);

  // RESIZE: MouseDown on handles
  const onResizeStart = (edge) => (e) => {
    e.preventDefault();
    e.stopPropagation();

    TIMELINE_STATE.isDragging = true;
    TIMELINE_STATE.resizingItemId = item.id;
    TIMELINE_STATE.resizeEdge = edge;
    TIMELINE_STATE.resizeStartY = e.clientY;

    startY = e.clientY;
    startTop = parseFloat(itemEl.style.top);
    startDuration = item.duration_mins;

    itemEl.style.opacity = "0.9";
    itemEl.style.zIndex = "1000";

    document.addEventListener("mousemove", onResizeMove);
    document.addEventListener("mouseup", onResizeEnd);
  };

  const onResizeMove = (e) => {
    const deltaY = e.clientY - startY;
    const deltaMins = Math.round(deltaY / TIMELINE_CONFIG.pixelsPerMinute / TIMELINE_CONFIG.G) * TIMELINE_CONFIG.G;

    let newDuration;
    if (TIMELINE_STATE.resizeEdge === "bottom") {
      newDuration = startDuration + deltaMins;
    } else {
      newDuration = startDuration - deltaMins;
    }

    if (newDuration < TIMELINE_CONFIG.minDuration) {
      itemEl.style.border = "2px solid #ef4444";
      TIMELINE_STATE.previewValid = false;
      return;
    }

    // Preview the resize
    const timeline = timedToTimelineItems(STATE.timed, rehearsalNum);
    const resizedItem = timeline.find(i => i.id === item.id);

    if (resizedItem) {
      const result = calculateResizeResult(resizedItem, newDuration, TIMELINE_STATE.resizeEdge, timeline, rehearsalNum);

      if (result.valid) {
        itemEl.style.border = "2px solid #22c55e";
        TIMELINE_STATE.previewValid = true;
        TIMELINE_STATE.previewTimeline = result.timeline;

        // Update visual height
        const newHeight = newDuration * TIMELINE_CONFIG.pixelsPerMinute;
        itemEl.style.height = newHeight + "px";

        // If resizing from top, adjust position
        if (TIMELINE_STATE.resizeEdge === "top") {
          const deltaHeight = (newDuration - startDuration) * TIMELINE_CONFIG.pixelsPerMinute;
          itemEl.style.top = (startTop - deltaHeight) + "px";
        }
      } else {
        itemEl.style.border = "2px solid #ef4444";
        TIMELINE_STATE.previewValid = false;
        console.log("Invalid resize:", result.errors);
      }
    }
  };

  const onResizeEnd = (e) => {
    document.removeEventListener("mousemove", onResizeMove);
    document.removeEventListener("mouseup", onResizeEnd);

    itemEl.style.opacity = "1";
    itemEl.style.zIndex = "10";
    itemEl.style.border = "";

    if (TIMELINE_STATE.previewValid && TIMELINE_STATE.previewTimeline) {
      // Apply the resize
      const beforeState = deepCopy(STATE.timed);

      // Update STATE.timed
      const updatedItems = timelineItemsToTimed(TIMELINE_STATE.previewTimeline);
      updatedItems.forEach(updated => {
        const idx = STATE.timed.findIndex(t => t._index === updated._index);
        if (idx !== -1) {
          STATE.timed[idx]['Time in Rehearsal'] = updated['Time in Rehearsal'];
          STATE.timed[idx]['Rehearsal Time (minutes)'] = updated['Rehearsal Time (minutes)'];
        }
      });

      const afterState = deepCopy(STATE.timed);

      // Push to history
      pushToHistory("resize", beforeState, afterState);

      // Save and re-render
      saveTimelineToBackend();
      renderTimelineEditor();
    } else {
      // Revert visual changes
      itemEl.style.height = (startDuration * TIMELINE_CONFIG.pixelsPerMinute) + "px";
      itemEl.style.top = startTop + "px";
      itemEl.style.animation = "shake 0.3s";
      setTimeout(() => itemEl.style.animation = "", 300);
    }

    TIMELINE_STATE.isDragging = false;
    TIMELINE_STATE.resizingItemId = null;
    TIMELINE_STATE.resizeEdge = null;
    TIMELINE_STATE.previewValid = false;
    TIMELINE_STATE.previewTimeline = null;
  };

  resizeTop.addEventListener("mousedown", onResizeStart("top"));
  resizeBottom.addEventListener("mousedown", onResizeStart("bottom"));
}

// Make functions available globally
window.renderTimelineEditor = renderTimelineEditor;
window.timelineUndo = timelineUndo;
window.timelineRedo = timelineRedo;
window.revertToOriginal = revertToOriginal;
