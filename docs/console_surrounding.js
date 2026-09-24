(() => {
  const MAP = {2: 'TRANSPORT', 3: 'LANDMARK', 5: 'SHOPPING'};
  const laNhom = (v) => Array.isArray(v) && v.length
    && v.every(g => g && typeof g === 'object' && 'id' in g && Array.isArray(g.places));

  const inRa = (ds, nguon) => {
    console.log(`%c${nguon} — ${ds.length} nhóm`, 'color:#0064ff;font-weight:bold');
    console.log('Các mã mục:', ds.map(g => g.id));
    console.table(ds.map(g => ({
      ma: g.id, section_type: MAP[g.id] || 'OTHER <- CHUA MAP',
      ten: g.name, so_dia_diem: (g.places || []).length,
    })));
    window.__nearby = ds;
    console.log('Chi tiết: window.__nearby');
  };

  // --- lục trong state của React trên cây DOM đã render ---
  const daXem = new Set();
  const lucSau = (o, sau) => {
    if (!o || typeof o !== 'object' || sau > 12 || daXem.has(o)) return null;
    daXem.add(o);
    if (laNhom(o.placeInfoList)) return o.placeInfoList;
    if (laNhom(o)) return o;
    for (const k in o) {
      let v; try { v = o[k]; } catch { continue; }
      const found = lucSau(v, sau + 1);
      if (found) return found;
    }
    return null;
  };

  for (const el of document.querySelectorAll('*')) {
    for (const k of Object.keys(el)) {
      if (!k.startsWith('__reactFiber$') && !k.startsWith('__reactProps$')) continue;
      let f = el[k];
      for (let i = 0; f && i < 30; i++, f = f.return) {
        const found = lucSau(f.memoizedProps, 0) || lucSau(f.memoizedState, 0);
        if (found) return inRa(found, 'React state');
      }
    }
  }

  // --- không thấy thì nghe API cho lần gọi sau ---
  console.log('%cKhông tìm thấy trong state. Đang nghe API…', 'color:#ff8800;font-weight:bold');
  const goc = window.fetch;
  window.fetch = async function (...a) {
    const r = await goc.apply(this, a);
    if (String(a[0]?.url || a[0] || '').includes('ctGetNearbyPlaceInfo')) {
      r.clone().json().then(j => { window.fetch = goc; inRa(j.data?.placeInfoList || j.placeInfoList || [], 'API'); });
    }
    return r;
  };
  console.log('→ Bấm tab "Vị trí" hoặc "Xem Trên Bản Đồ" để trang gọi lại API.');
})();
