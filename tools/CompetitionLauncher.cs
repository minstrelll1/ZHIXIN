using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class CompetitionLauncher
{
    [STAThread]
    private static void Main()
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string script = Path.Combine(root, "tools", "launch_ground_app.ps1");
            if (!File.Exists(script)) throw new Exception("请把此应用保留在 competition_development 项目目录内。");
            Process.Start(new ProcessStartInfo("powershell.exe", "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"")
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            });
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "智信竞赛程序", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
