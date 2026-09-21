/* Tab "Crawl & Theo dõi": đọc độ phủ từ DB và điều khiển job crawl. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 1500;          // nhịp hỏi trạng thái job khi đang chạy
  let pollTimer = null;

  // ---------------------------------------------------------------- tiện ích
  async function api(path, options) {
    const res = await fetch(path, options);
    let data = {};
    try { data = await res.json(); } catch (_) { /* body rỗng */ }
    if (!res.ok) throw new Error(data.error || `Lỗi ${res.status}`);
    return data;
  }

  function level(percent) {
    if (percent >= 85) return 'lvl-high';
    if (percent >= 50) return 'lvl-mid';
    return 'lvl-low';
  }

  const fmt = (n) => Number(n).toLocaleString('vi-VN');

  // ------------------------------------------------------------- trạng thái DB
  async function refreshHealth() {
    const box = $('db-status');
    try {
      const data = await api('/api/health');
      box.classList.add('ok');
      box.querySelector('span').textContent = data.database || 'Đã kết nối';
    } catch (err) {
      box.classList.remove('ok');
      box.querySelector('span').textContent = 'Mất kết nối DB';
    }
  }

  // ------------------------------------------------------------------ độ phủ
  async function refreshCoverage() {
    const locale = $('coverage-locale').value;
    const list = $('coverage-list');
    try {
      const data = await api(`/api/crawl/coverage?locale=${encodeURIComponent(locale)}`);
      const cov = data.coverage;

      $('metric-hotels').textContent = fmt(cov.total_hotels);
      $('metric-overall').textContent = `${cov.overall_percent}%`;
      $('metric-locale').textContent = locale === 'en' ? 'EN' : 'VI';

      list.innerHTML = '';
      cov.sections.forEach((row) => {
        const el = document.createElement('div');
        el.className = 'coverage-row';
        el.innerHTML = `
          <div class="name"></div>
          <div class="bar ${level(row.percent)}"><i style="width:${Math.min(row.percent, 100)}%"></i></div>
          <div class="count"><b>${row.percent}%</b> · thiếu ${fmt(row.missing)}</div>`;
        el.querySelector('.name').textContent = row.label;   // tránh XSS từ nhãn
        list.appendChild(el);
      });

      const cities = $('city-list');
      cities.innerHTML = '';
      (data.cities || []).forEach((city) => {
        const chip = document.createElement('div');
        chip.className = 'city-chip' + (city.hotels ? '' : ' empty');
        chip.innerHTML = `<strong>${fmt(city.hotels)}</strong><span></span>`;
        chip.querySelector('span').textContent = city.name || city.trip_location_id;
        cities.appendChild(chip);
      });
      if (!cities.children.length) cities.innerHTML = '<p class="muted">Chưa có thành phố nào.</p>';
    } catch (err) {
      list.innerHTML = `<p class="muted">Không đọc được độ phủ: ${err.message}</p>`;
    }
  }

  // -------------------------------------------------------------------- job
  function paramsFromForm() {
    const locale = $('job-locale').value;
    const limit = $('job-limit').value;
    return {
      locale,
      currency: locale === 'en-US' ? 'USD' : 'VND',
      city_id: Number($('job-city').value || 301),
      workers: Number($('job-workers').value || 2),
      limit: limit ? Number(limit) : null,
      missing_only: $('job-missing').checked,
      apply: $('job-apply').checked,
    };
  }

  function renderButtons(jobs) {
    const box = $('job-buttons');
    if (box.dataset.ready) return;          // chỉ dựng một lần
    box.dataset.ready = '1';
    jobs.forEach((job) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-primary';
      btn.dataset.job = job.key;
      btn.innerHTML = '<span></span><small></small>';
      btn.querySelector('span').textContent = job.label;
      btn.querySelector('small').textContent = job.note;
      btn.addEventListener('click', () => startJob(job.key, btn));
      box.appendChild(btn);
    });
  }

  async function startJob(key, btn) {
    const label = btn.querySelector('span').textContent;
    if (!confirm(`Chạy "${label}"?\n\nJob gọi ra Trip.com sẽ mất nhiều thời gian và tự dừng nếu bị chặn.`)) return;
    try {
      await api('/api/crawl/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job: key, params: paramsFromForm() }),
      });
      refreshJobs();
    } catch (err) {
      alert(`Không chạy được: ${err.message}`);
    }
  }

  async function stopJob() {
    if (!confirm('Dừng job đang chạy?\n\nDữ liệu đã cào xong vẫn được giữ.')) return;
    try {
      await api('/api/crawl/stop', { method: 'POST' });
      refreshJobs();
    } catch (err) {
      alert(err.message);
    }
  }

  function renderCurrent(job) {
    const state = $('job-state');
    const stopBtn = $('stop-job');
    const log = $('job-log');

    if (!job) {
      state.textContent = 'Rảnh';
      state.className = 'badge badge-idle';
      stopBtn.disabled = true;
      $('job-progress').textContent = '—';
      $('job-bar').style.width = '0%';
      return;
    }

    const running = job.running;
    state.textContent = running ? (job.stopping ? 'Đang dừng…' : `Đang chạy: ${job.label}`)
                                : `Xong: ${job.label}`;
    state.className = 'badge ' + (running ? 'badge-run' : 'badge-idle');
    stopBtn.disabled = !running;

    $('job-command').textContent = job.command || '';
    if (job.total) {
      $('job-bar').style.width = `${Math.min(job.percent || 0, 100)}%`;
      $('job-progress').textContent = `${fmt(job.done)}/${fmt(job.total)} · ${job.percent}%`;
    } else {
      $('job-bar').style.width = running ? '100%' : '0%';
      $('job-progress').textContent = running ? 'đang chạy…' : '—';
    }

    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    log.innerHTML = '';
    (job.lines || []).forEach((line) => {
      const row = document.createElement('div');
      // Dòng báo bị Trip.com chặn thì bôi đỏ cho dễ thấy.
      if (/chặn|Antibot|SpiderAction/i.test(line)) row.className = 'blocked';
      row.textContent = line;
      log.appendChild(row);
    });
    if (!job.lines || !job.lines.length) log.textContent = 'Chưa có log.';
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function renderHistory(history) {
    const box = $('job-history');
    box.innerHTML = '';
    if (!history || !history.length) {
      box.innerHTML = '<p class="muted">Chưa có.</p>';
      return;
    }
    history.forEach((job) => {
      const row = document.createElement('div');
      row.className = 'history-row';
      const ok = job.returncode === 0;
      row.innerHTML = `<span class="${ok ? 'ok' : 'fail'}">${ok ? '✓' : '✕'}</span>
        <strong></strong><time></time><span class="muted"></span>`;
      row.querySelector('strong').textContent = job.label;
      row.querySelector('time').textContent = `${job.started_at} → ${job.finished_at || '—'}`;
      row.querySelector('.muted').textContent =
        job.total ? `${fmt(job.done)}/${fmt(job.total)}` : `mã thoát ${job.returncode}`;
      box.appendChild(row);
    });
  }

  async function refreshJobs() {
    try {
      const data = await api('/api/crawl/jobs');
      renderButtons(data.jobs || []);
      renderCurrent(data.current);
      renderHistory(data.history);

      const running = data.current && data.current.running;
      document.querySelectorAll('#job-buttons .btn').forEach((b) => { b.disabled = !!running; });

      clearTimeout(pollTimer);
      if (running) {
        pollTimer = setTimeout(refreshJobs, POLL_MS);
      } else if (data.current && !data.current.finishedHandled) {
        // Job vừa xong: cập nhật lại độ phủ để thấy kết quả ngay.
        data.current.finishedHandled = true;
        refreshCoverage();
      }
    } catch (err) {
      $('job-state').textContent = 'Không đọc được trạng thái job';
      $('job-state').className = 'badge badge-warn';
    }
  }

  // ------------------------------------------------------------------- khởi động
  $('refresh-coverage').addEventListener('click', refreshCoverage);
  $('coverage-locale').addEventListener('change', refreshCoverage);
  $('stop-job').addEventListener('click', stopJob);

  refreshHealth();
  refreshCoverage();
  refreshJobs();
  setInterval(refreshHealth, 30000);
})();
