/* 亲子双罐激励系统 —— 轻量交互（原生 JS，无任何依赖） */
(function () {
  "use strict";

  // 提交前二次确认：表单带 data-confirm 属性时先弹窗
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (msg) {
      if (!window.confirm(msg)) {
        e.preventDefault();
      }
    }
  });

  // 管理页手工调整：点击快捷金额，填入金额输入框
  document.addEventListener("click", function (e) {
    var t = e.target;
    var chip = t && t.closest ? t.closest(".chip") : null;
    if (!chip) return;
    var form = chip.closest("form");
    var input = form ? form.querySelector(".amt-input") : null;
    if (input) {
      input.value = chip.getAttribute("data-amount");
      input.focus();
    }
  });
})();
