class DateRangePicker {
  constructor({ container, afterInputId, beforeInputId, onChange }) {
    this.afterInput = document.getElementById(afterInputId);
    this.beforeInput = document.getElementById(beforeInputId);
    this.onChange = onChange || (() => {});

    this.startDate = this.afterInput.value ? new Date(this.afterInput.value + 'T00:00:00') : null;
    this.endDate   = this.beforeInput.value ? new Date(this.beforeInput.value + 'T00:00:00') : null;
    this.hoverDate = null;
    this.selecting = false; // false=picking start, true=picking end

    this._build(container);
    this._renderCalendar();
    this._updateTrigger();
    this._bindOutside();
  }

  // ── Helpers ──────────────────────────────────────────────
  static _d(y, m, d) { const dt = new Date(y, m, d); return dt; }
  static _today()    { const t = new Date(); return DateRangePicker._d(t.getFullYear(), t.getMonth(), t.getDate()); }
  static _ymd(dt)   { if (!dt) return ''; return `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`; }
  static _fmt(dt)   {
    if (!dt) return '—';
    const ms = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${ms[dt.getMonth()]} ${dt.getDate()}, ${dt.getFullYear()}`;
  }
  static _mmddyyyy(dt) {
    if (!dt) return '';
    return `${String(dt.getMonth()+1).padStart(2,'0')}/${String(dt.getDate()).padStart(2,'0')}/${dt.getFullYear()}`;
  }
  static _parseMDY(s) {
    const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec((s||'').trim());
    if (!m) return null;
    const dt = new Date(+m[3], +m[1]-1, +m[2]);
    return isNaN(dt) ? null : dt;
  }
  _inRange(dt, s, e) {
    if (!s || !e || !dt) return false;
    const [a, b] = s <= e ? [s,e] : [e,s];
    return dt >= a && dt <= b;
  }

  // ── Build DOM ─────────────────────────────────────────────
  _build(container) {
    container.innerHTML = '';
    container.style.position = 'relative';

    // Trigger
    this.trigger = document.createElement('div');
    this.trigger.className = 'drp-trigger';
    this.trigger.innerHTML = `
      <div class="date-filter">
        <span class="range">
          <span class="start-date"></span>
          <span class="sep"> to </span>
          <span class="end-date"></span>
        </span>
        <span class="toggle">&#128197;</span>
      </div>`;
    this.trigger.addEventListener('click', () => this._togglePopover());
    container.appendChild(this.trigger);

    // Popover
    this.popover = document.createElement('div');
    this.popover.className = 'date-picker';
    this.popover.style.display = 'none';

    // Left panel
    const left = document.createElement('div');
    left.className = 'drp-left';

    // Header row (weekdays)
    const header = document.createElement('div');
    header.className = 'weekdays';
    header.innerHTML = `
      <span class="header-cell month"></span>
      <span class="header-cell">Mo</span>
      <span class="header-cell">Tu</span>
      <span class="header-cell">We</span>
      <span class="header-cell">Th</span>
      <span class="header-cell">Fr</span>
      <span class="header-cell weekend">Sa</span>
      <span class="header-cell weekend">Su</span>`;
    left.appendChild(header);

    const daysWrap = document.createElement('div');
    daysWrap.className = 'days-container';
    this.daysInner = document.createElement('div');
    this.daysInner.className = 'tse-content';
    daysWrap.appendChild(this.daysInner);
    left.appendChild(daysWrap);
    this.daysWrap = daysWrap;
    this.popover.appendChild(left);

    // Right panel
    const right = document.createElement('div');
    right.className = 'drp-right';

    const predef = document.createElement('div');
    predef.className = 'predefined';
    const today = DateRangePicker._today();
    const shortcuts = [
      { label: 'Today',      fn: () => [today, today] },
      { label: 'Yesterday',  fn: () => { const y = new Date(today); y.setDate(today.getDate()-1); return [y,y]; } },
      { label: 'This Week',  fn: () => { const mon = new Date(today); mon.setDate(today.getDate() - ((today.getDay()+6)%7)); return [mon, today]; } },
      { label: 'Last Week',  fn: () => {
        const mon = new Date(today); mon.setDate(today.getDate() - ((today.getDay()+6)%7) - 7);
        const sun = new Date(mon); sun.setDate(mon.getDate()+6);
        return [mon, sun];
      }},
      { label: 'This Month', fn: () => [DateRangePicker._d(today.getFullYear(), today.getMonth(), 1), today] },
      { label: 'Last Month', fn: () => {
        const f = DateRangePicker._d(today.getFullYear(), today.getMonth()-1, 1);
        const l = DateRangePicker._d(today.getFullYear(), today.getMonth(), 0);
        return [f, l];
      }},
    ];
    const ul = document.createElement('ul');
    shortcuts.forEach(sc => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.className = 'range-button';
      btn.type = 'button';
      btn.textContent = sc.label;
      btn.addEventListener('click', () => {
        const [s, e] = sc.fn();
        this._applyRange(s, e);
        this._closePopover();
      });
      li.appendChild(btn);
      ul.appendChild(li);
    });
    predef.appendChild(ul);
    right.appendChild(predef);

    // Manual inputs
    const inp = document.createElement('div');
    inp.className = 'drp-inputs';
    inp.innerHTML = `
      <p><input type="text" class="date range-start" placeholder="MM/DD/YYYY" id="drp-start-txt"></p>
      <h4>To:</h4>
      <p><input type="text" class="date range-end" placeholder="MM/DD/YYYY" id="drp-end-txt"></p>
      <p><button type="button" class="apply-button" id="drp-apply">Apply</button></p>`;
    right.appendChild(inp);
    this.popover.appendChild(right);
    container.appendChild(this.popover);

    this.startTxt = inp.querySelector('#drp-start-txt');
    this.endTxt   = inp.querySelector('#drp-end-txt');
    inp.querySelector('#drp-apply').addEventListener('click', () => this._applyFromText());
  }

  // ── Calendar rendering ────────────────────────────────────
  _renderCalendar() {
    const today = DateRangePicker._today();
    const refDate = this.startDate || today;

    // Generate ~14 months of weeks: 7 months before, current, 6 after
    const startMonth = new Date(refDate.getFullYear(), refDate.getMonth() - 7, 1);

    const weeks = [];
    // Find Monday of the week containing startMonth
    let cur = new Date(startMonth);
    cur.setDate(1);
    const dow = (cur.getDay() + 6) % 7; // 0=Mon
    cur.setDate(cur.getDate() - dow);

    const endBound = new Date(refDate.getFullYear(), refDate.getMonth() + 7, 1);
    let totalWeeks = 0;
    const MAX_WEEKS = 66;
    while (cur < endBound && totalWeeks < MAX_WEEKS) {
      const week = [];
      for (let d = 0; d < 7; d++) {
        week.push(new Date(cur));
        cur.setDate(cur.getDate() + 1);
      }
      weeks.push(week);
      totalWeeks++;
    }

    this._weeks = weeks;
    this.daysInner.innerHTML = '';
    this.daysInner.style.height = (weeks.length * 32) + 'px';

    let prevMonthKey = null;
    weeks.forEach((week, wi) => {
      const weekEl = document.createElement('div');
      weekEl.className = 'week';

      // Month label cell: label the first week of each new month, using
      // the Thursday (week[3]) as the canonical "month this week belongs to".
      const labelCell = document.createElement('div');
      labelCell.className = 'day month-label-cell';
      labelCell.style.width = '32px';
      const refDay = week[3];
      const monthKey = refDay.getFullYear() * 12 + refDay.getMonth();
      if (monthKey !== prevMonthKey) {
        const mns = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const sp = document.createElement('span');
        sp.className = 'month-name';
        sp.textContent = mns[refDay.getMonth()] + " " + String(refDay.getFullYear()).slice(2);
        labelCell.appendChild(sp);
        prevMonthKey = monthKey;
      }
      weekEl.appendChild(labelCell);

      week.forEach((dt, di) => {
        const dayEl = document.createElement('div');
        const isToday = dt.toDateString() === today.toDateString();

        const effEnd   = this.selecting && this.hoverDate ? this.hoverDate : this.endDate;
        const inRange  = this._inRange(dt, this.startDate, effEnd);
        const isStart  = this.startDate && dt.toDateString() === this.startDate.toDateString();
        const isEnd    = effEnd && dt.toDateString() === effEnd.toDateString();

        const cls = ['day'];
        if (isToday)  cls.push('today');
        if (inRange)  cls.push('inrange');
        if (isStart)  cls.push('pos-start');
        if (isEnd)    cls.push('pos-end');

        dayEl.className = cls.join(' ');
        dayEl.innerHTML = `<span class="adjust-left"></span><span class="adjust-right"></span>${dt.getDate()}`;

        dayEl.addEventListener('click', () => this._clickDay(dt));
        dayEl.addEventListener('mouseenter', () => {
          if (this.selecting) {
            this.hoverDate = dt;
            this._refreshDayClasses();
          }
        });
        weekEl.appendChild(dayEl);
      });
      this.daysInner.appendChild(weekEl);
    });

    // Scroll to current month
    this._scrollToMonth(refDate);
  }

  _scrollToMonth(dt) {
    const today = DateRangePicker._today();
    const refDate = dt || today;
    // Find the week index for the first week of refDate's month
    const monthStart = new Date(refDate.getFullYear(), refDate.getMonth(), 1);
    let targetWeek = 0;
    for (let i = 0; i < this._weeks.length; i++) {
      const w = this._weeks[i];
      // Week contains the 1st of the month
      if (w.some(d => d.getFullYear() === monthStart.getFullYear() &&
                       d.getMonth() === monthStart.getMonth() &&
                       d.getDate() === 1)) {
        targetWeek = i;
        break;
      }
    }
    const scrollTop = Math.max(0, (targetWeek - 1) * 32);
    this.daysWrap.scrollTop = scrollTop;
  }

  _refreshDayClasses() {
    const today = DateRangePicker._today();
    const effEnd = this.selecting && this.hoverDate ? this.hoverDate : this.endDate;
    const allDays = this.daysInner.querySelectorAll('.day:not(.month-label-cell)');
    let wi = 0, di = 0;
    allDays.forEach((dayEl, idx) => {
      const week = this._weeks[Math.floor(idx / 7)];
      if (!week) return;
      const dt = week[idx % 7];
      if (!dt) return;

      const isToday = dt.toDateString() === today.toDateString();
      const inRange = this._inRange(dt, this.startDate, effEnd);
      const isStart = this.startDate && dt.toDateString() === this.startDate.toDateString();
      const isEnd   = effEnd && dt.toDateString() === effEnd.toDateString();

      const cls = ['day'];
      if (isToday)  cls.push('today');
      if (inRange)  cls.push('inrange');
      if (isStart)  cls.push('pos-start');
      if (isEnd)    cls.push('pos-end');
      dayEl.className = cls.join(' ');
    });
  }

  // ── Interaction ──────────────────────────────────────────
  _clickDay(dt) {
    if (!this.selecting) {
      // First click: set start
      this.startDate = dt;
      this.endDate = null;
      this.selecting = true;
    } else {
      // Second click: set end, swap if needed
      this.endDate = dt;
      if (this.endDate < this.startDate) {
        [this.startDate, this.endDate] = [this.endDate, this.startDate];
      }
      this.selecting = false;
      this.hoverDate = null;
    }
    this._refreshDayClasses();
    this._updateTrigger();
    this._updateTextInputs();
  }

  _applyFromText() {
    const s = DateRangePicker._parseMDY(this.startTxt.value);
    const e = DateRangePicker._parseMDY(this.endTxt.value);
    if (s) this.startDate = s;
    if (e) this.endDate   = e;
    if (this.startDate && this.endDate && this.endDate < this.startDate) {
      [this.startDate, this.endDate] = [this.endDate, this.startDate];
    }
    this._applyRange(this.startDate, this.endDate);
    this._closePopover();
  }

  _applyRange(s, e) {
    this.startDate = s;
    this.endDate   = e;
    this.selecting = false;
    this.hoverDate = null;
    this._refreshDayClasses();
    this._updateTrigger();
    this._updateTextInputs();
    // Write into the hidden form inputs
    this.afterInput.value  = DateRangePicker._ymd(s);
    this.beforeInput.value = DateRangePicker._ymd(e);
    this.onChange(s, e);
  }

  _updateTrigger() {
    this.trigger.querySelector('.start-date').textContent = DateRangePicker._fmt(this.startDate);
    this.trigger.querySelector('.end-date').textContent   = DateRangePicker._fmt(this.endDate);
  }

  _updateTextInputs() {
    this.startTxt.value = DateRangePicker._mmddyyyy(this.startDate);
    this.endTxt.value   = DateRangePicker._mmddyyyy(this.endDate);
    // Also update hidden inputs
    this.afterInput.value  = DateRangePicker._ymd(this.startDate);
    this.beforeInput.value = DateRangePicker._ymd(this.endDate);
  }

  // ── Popover open/close ────────────────────────────────────
  _togglePopover() {
    if (this.popover.style.display === 'none') {
      this._openPopover();
    } else {
      this._closePopover();
    }
  }

  _openPopover() {
    this.popover.style.display = 'flex';
    this._renderCalendar();
    this._updateTextInputs();
  }

  _closePopover() {
    this.popover.style.display = 'none';
  }

  _bindOutside() {
    document.addEventListener('click', (e) => {
      if (!this.trigger.closest('.drp-wrap').contains(e.target)) {
        this._closePopover();
      }
    });
  }
}

document.addEventListener("DOMContentLoaded", function() {
  const c = document.getElementById("drp-container");
  if (c) new DateRangePicker({ container: c, afterInputId: "after-input", beforeInputId: "before-input", onChange: function() {} });
});