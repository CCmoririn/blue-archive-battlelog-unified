const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = {};
const field = {value:'ヒフミ(レーシング)(確認)', selectionStart:4, selectionEnd:4,
  addEventListener:(name,callback)=>listeners[name]=callback,
  setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}};
const document = {querySelectorAll:selector=>selector==='[data-fullwidth-parens]'?[field]:[],getElementById:()=>null};
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../admin_static/admin.js'),'utf8'),{document,URL,window:{}});
listeners.input({isComposing:false});
assert.equal(field.value,'ヒフミ（レーシング）（確認）');
assert.equal(field.selectionStart,4);
field.value='お知らせ(入力中)';
listeners.input({isComposing:true});
assert.equal(field.value,'お知らせ(入力中)');
listeners.compositionend();
assert.equal(field.value,'お知らせ（入力中）');
console.log('Admin text normalization regressions passed.');
