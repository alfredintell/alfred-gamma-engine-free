// ==UserScript==
// @name         Alfred Gamma Panel Overlay
// @namespace    alfred-intelligence
// @version      0.1
// @description  Shows Alfred Gamma JSON levels as a floating panel on TradingView. This does not draw price-axis Pine lines.
// @match        https://www.tradingview.com/*
// @grant        GM_xmlhttpRequest
// @connect      raw.githubusercontent.com
// ==/UserScript==
(function(){
  const RAW_JSON_URL = 'PASTE_RAW_GITHUB_JSON_URL_HERE';
  const panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;z-index:999999;right:20px;bottom:90px;background:#08111f;color:#e5eefc;border:1px solid #2a4166;border-radius:12px;padding:12px;font:12px Inter,Arial;max-width:280px;box-shadow:0 10px 30px rgba(0,0,0,.35)';
  panel.innerHTML = '<b>Alfred Gamma</b><br>Loading...';
  document.body.appendChild(panel);
  function render(d){
    panel.innerHTML = `<b>Alfred Gamma ${d.market}</b><br><span style="color:#9fb0c8">${d.source}</span><br>` +
      `CR: <b>${d.call_wall ?? '-'}</b><br>PS: <b>${d.put_wall ?? '-'}</b><br>HVL: <b>${d.hvl_gamma_flip ?? '-'}</b><br>` +
      `0DTE CR: <b>${d.front_call_wall ?? '-'}</b><br>0DTE PS: <b>${d.front_put_wall ?? '-'}</b><br>` +
      `Regime: <b>${d.net_gex_regime}</b>`;
  }
  function load(){
    if(!RAW_JSON_URL || RAW_JSON_URL.includes('PASTE_')) { panel.innerHTML = '<b>Alfred Gamma</b><br>Paste RAW_JSON_URL in the userscript.'; return; }
    GM_xmlhttpRequest({method:'GET', url:RAW_JSON_URL, onload:r=>{ try{ render(JSON.parse(r.responseText)); } catch(e){ panel.innerHTML='Alfred Gamma JSON error'; } }});
  }
  load(); setInterval(load, 5*60*1000);
})();
