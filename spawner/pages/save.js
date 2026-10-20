var SAVE_KEY='handover-save';
function loadSave(){var lv={};
  try{var d=JSON.parse(localStorage.getItem(SAVE_KEY));
    if(d&&d.levels&&typeof d.levels==='object')for(var k in d.levels){var v=d.levels[k];
      if(/^([1-9]|1[0-9]|20)$/.test(k)&&typeof v==='string'&&/^[A-Za-z0-9]{1,64}$/.test(v))lv[k]=v;}
  }catch(e){}
  return lv;}
function storeSave(lv){try{localStorage.setItem(SAVE_KEY,JSON.stringify({v:1,levels:lv}));}catch(e){}}
function topLevel(lv){var m=0;for(var k in lv)m=Math.max(m,+k);return m;}
