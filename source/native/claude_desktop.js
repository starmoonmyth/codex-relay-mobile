ObjC.import('Foundation');

function run() {
  let submitted = false;
  try {
    const bytes = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
    const request = JSON.parse(ObjC.unwrap($.NSString.alloc.initWithDataEncoding(bytes, $.NSUTF8StringEncoding)));
    const system = Application('System Events');
    const processes = system.processes.whose({name: 'Claude'})();
    if (processes.length !== 1) throw Error('请打开 Claude 桌面，并保持 Mac 登录、解锁。');
    const process = processes[0];
    function attribute(element, name) {
      try { return element.attributes.byName(name).value(); } catch (_) { return null; }
    }
    function contents(element) { return element.entireContents(); }
    function checkBranch() {
      const bytes = $.NSData.dataWithContentsOfFile(request.view_path);
      const view = JSON.parse(ObjC.unwrap($.NSString.alloc.initWithDataEncoding(bytes, $.NSUTF8StringEncoding)));
      if (view.sessionId !== request.native_id || view.cliSessionId !== request.expected_cli ||
          (view.lastAssistantUuid || '') !== request.expected_head) {
        throw Error('Claude 原聊天消息链已变化，请刷新后发送；本次未点击发送。');
      }
    }
    function label(element) { return String(attribute(element, 'AXDescription') || attribute(element, 'AXTitle') || ''); }
    function document() {
      const target = 'https://claude.ai/epitaxy/' + request.native_id;
      const found = [];
      for (const window of process.windows()) {
        for (const element of contents(window)) {
          if (attribute(element, 'AXRole') !== 'AXWebArea') continue;
          const url = String(attribute(element, 'AXURL') || attribute(element, 'AXDocument') || '');
          if (url.split(/[?#]/)[0].replace(/\/$/, '') === target) found.push(element);
        }
      }
      if (found.length !== 1) throw Error('Claude 当前窗口与所选聊天不一致，本次未发送。');
      return found[0];
    }
    function composer(doc) {
      const found = contents(doc).filter(e => ['AXTextArea', 'AXTextField'].includes(attribute(e, 'AXRole')) &&
        ['提示词', 'Prompt', 'Reply to Claude', 'Message Claude'].includes(label(e)));
      if (found.length !== 1) throw Error('找不到唯一的 Claude 输入框，请先处理桌面弹窗。');
      return found[0];
    }
    let doc;
    for (let attempt = 0; attempt < 40; attempt++) {
      delay(0.25);
      try { doc = document(); composer(doc); break; } catch (_) { doc = null; }
    }
    if (!doc) throw Error('Claude 未打开指定聊天；Mac 需在系统设置中允许消息助手的辅助功能和自动化权限。');
    checkBranch();
    let editor = composer(doc);
    if (String(attribute(editor, 'AXValue') || '').trim()) throw Error('Claude 原聊天有未发送草稿，请先在 Mac 处理。');
    process.frontmost = true;
    const models = contents(doc).filter(e => attribute(e, 'AXRole') === 'AXButton' && /^(模型：|Model:)/.test(label(e)));
    if (models.length !== 1) throw Error('无法确认 Claude 桌面当前模型，本次未发送。');
    const actual = label(models[0]);
    editor = composer(document());
    if (String(attribute(editor, 'AXValue') || '').trim()) throw Error('Claude 输入框已变化，本次未发送。');
    editor.attributes.byName('AXValue').value = request.text;
    delay(0.2);
    doc = document();
    if (String(attribute(composer(doc), 'AXValue')).replace(/\r\n/g, '\n') !== request.text.replace(/\r\n/g, '\n')) {
      throw Error('草稿内容未能核对，请检查 Claude 输入框；本次未点击发送。');
    }
    const buttons = contents(doc).filter(e => attribute(e, 'AXRole') === 'AXButton' &&
      attribute(e, 'AXEnabled') !== false && ['发送', '发送消息', 'Send', 'Send message', 'Send now'].includes(label(e)));
    if (buttons.length !== 1) throw Error('Claude 发送按钮不可用，消息保留在原聊天草稿中。');
    document(); checkBranch();
    submitted = true;
    buttons[0].actions.byName('AXPress').perform();
    return JSON.stringify({ok: true, model: actual});
  } catch (error) {
    return JSON.stringify({ok: false, error: String(error.message || error), uncertain: submitted});
  }
}
