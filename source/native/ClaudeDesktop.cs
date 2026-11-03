using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Forms;

internal static class ClaudeDesktop {
    static bool submitted;
    static AutomationElement window;
    static string nativeId;
    static Dictionary<string,object> payload;
    static readonly JavaScriptSerializer json = new JavaScriptSerializer();
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int command);

    static AutomationElement[] Find(AutomationElement root, ControlType type) {
        return root.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, type))
            .Cast<AutomationElement>().ToArray();
    }
    static string Value(AutomationElement e) {
        object p;
        if(e.TryGetCurrentPattern(ValuePattern.Pattern,out p)) return ((ValuePattern)p).Current.Value;
        if(e.TryGetCurrentPattern(TextPattern.Pattern,out p)) return ((TextPattern)p).DocumentRange.GetText(-1).TrimEnd('\r','\n');
        throw new Exception("无法读取 Claude 输入框，本次未发送。");
    }
    static AutomationElement Document() {
        var target = "https://claude.ai/epitaxy/" + nativeId;
        var documents = Find(window,ControlType.Document).Where(e=> {
            try { return new Uri(Value(e)).GetLeftPart(UriPartial.Path).TrimEnd('/') == target; }
            catch { return false; }
        }).ToArray();
        if(documents.Length!=1) throw new Exception("Claude 当前窗口与所选聊天不一致，本次未发送。");
        return documents[0];
    }
    static AutomationElement Composer(AutomationElement doc) {
        var editors=Find(doc,ControlType.Edit).Where(e=> e.Current.IsEnabled &&
            new[]{"提示词","Prompt","Reply to Claude","Message Claude"}.Contains(e.Current.Name)).ToArray();
        if(editors.Length!=1) throw new Exception("找不到唯一的 Claude 输入框，请先完成桌面的审批或弹窗。");
        return editors[0];
    }
    static void Invoke(AutomationElement e) {
        object pattern;
        if(!e.TryGetCurrentPattern(InvokePattern.Pattern,out pattern)) throw new Exception("Claude 控件暂不可操作。");
        ((InvokePattern)pattern).Invoke();
    }
    static string Model(AutomationElement doc) {
        var buttons=Find(doc,ControlType.Button).Where(e=>e.Current.Name.StartsWith("模型：") || e.Current.Name.StartsWith("Model:")).ToArray();
        if(buttons.Length!=1) throw new Exception("无法确认 Claude 桌面当前模型，本次未发送。");
        return buttons[0].Current.Name;
    }
    static void Foreground() {
        var handle=new IntPtr(window.Current.NativeWindowHandle);
        ShowWindow(handle,9); SetForegroundWindow(handle); Thread.Sleep(150);
        if(GetForegroundWindow()!=handle) throw new Exception("无法激活 Claude 窗口，请解锁电脑并关闭遮挡的弹窗。");
    }
    static void CheckBranch() {
        var view=json.Deserialize<Dictionary<string,object>>(System.IO.File.ReadAllText((string)payload["view_path"],Encoding.UTF8));
        object head;
        view.TryGetValue("lastAssistantUuid",out head);
        if((string)view["sessionId"]!=nativeId || (string)view["cliSessionId"]!=(string)payload["expected_cli"] ||
            (head as string ?? "")!=(string)payload["expected_head"])
            throw new Exception("Claude 原聊天消息链已变化，请刷新后发送；本次未点击发送。");
    }
    static void Send(Dictionary<string,object> request) {
        payload=request;
        nativeId=(string)request["native_id"];
        string text=(string)request["text"];
        var processes=Process.GetProcessesByName("Claude").Where(p=> {
            try {return p.MainWindowHandle!=IntPtr.Zero;} catch{return false;}
        }).ToArray();
        if(processes.Length!=1) throw new Exception("请打开一个 Claude 桌面主窗口，并保持电脑登录、解锁。");
        window=AutomationElement.FromHandle(processes[0].MainWindowHandle);
        Process.Start(new ProcessStartInfo("claude://claude.ai/epitaxy/"+nativeId){UseShellExecute=true});
        AutomationElement doc=null;
        var deadline=DateTime.UtcNow.AddSeconds(15);
        while(DateTime.UtcNow<deadline) {
            Thread.Sleep(250);
            try {doc=Document();Composer(doc);break;} catch {doc=null;}
        }
        if(doc==null) throw new Exception("Claude 未打开指定聊天，请在桌面完成加载后重试。");
        CheckBranch();
        var editor=Composer(doc);
        if(!String.IsNullOrWhiteSpace(Value(editor))) throw new Exception("Claude 原聊天有未发送草稿，请先在电脑处理；手机消息尚未发送。");
        Foreground();
        var actual=Model(Document());
        editor=Composer(Document());
        if(!String.IsNullOrWhiteSpace(Value(editor))) throw new Exception("Claude 输入框已变化，本次未发送。");
        editor.SetFocus();
        object pattern;
        if(editor.TryGetCurrentPattern(ValuePattern.Pattern,out pattern) && !((ValuePattern)pattern).Current.IsReadOnly) {
            ((ValuePattern)pattern).SetValue(text);
        } else {
            // Paste keeps multiline text inside the editor. Enter is reserved
            // for the separately validated native Send action.
            IDataObject old=Clipboard.GetDataObject();
            try {
                Clipboard.SetText(text); SendKeys.SendWait("^v"); Thread.Sleep(250);
            } finally {
                if(Clipboard.ContainsText() && Clipboard.GetText()==text) {
                    if(old!=null) Clipboard.SetDataObject(old,true); else Clipboard.Clear();
                }
            }
        }
        Thread.Sleep(200);
        doc=Document();editor=Composer(doc);
        if(Value(editor).Replace("\r\n","\n")!=text.Replace("\r\n","\n")) throw new Exception("草稿内容未能核对，请检查 Claude 输入框；本次未点击发送。");
        var buttons=Find(doc,ControlType.Button).Where(e=>e.Current.IsEnabled &&
            new[]{"发送","发送消息","Send","Send message","Send now"}.Contains(e.Current.Name)).ToArray();
        if(buttons.Length!=1) throw new Exception("Claude 发送按钮不可用，消息保留在原聊天草稿中。");
        Document();CheckBranch();
        submitted=true; Invoke(buttons[0]);
        Console.Write(json.Serialize(new {ok=true,model=actual}));
    }
    [STAThread] public static int Main() {
        Console.InputEncoding=new UTF8Encoding(false);Console.OutputEncoding=new UTF8Encoding(false);
        try {
            Send(json.Deserialize<Dictionary<string,object>>(Console.In.ReadToEnd()));return 0;
        } catch(Exception error) {
            Console.Write(json.Serialize(new {ok=false,error=error.Message,uncertain=submitted}));return 1;
        }
    }
}
