import argparse
import pathlib
import platform
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination',required=True)
    parser.add_argument('--work-dir',required=True)
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parent
    work = pathlib.Path(args.work_dir).resolve()
    destination = pathlib.Path(args.destination).resolve()
    work.mkdir(parents=True,exist_ok=True)
    destination.mkdir(parents=True,exist_ok=True)
    data = [(str(root/'web'),'web'),(str(root/'peers.json'),'.'),(str(root/'licenses'),'licenses')]
    common = f"""a = Analysis([{str(root/'launcher.py')!r}],pathex=[{str(root)!r}],binaries=[],datas={data!r},
                 hiddenimports=['qrcode.image.svg'],hookspath=[],hooksconfig={{}},runtime_hooks=[],excludes=[],noarchive=False)
pyz = PYZ(a.pure)
"""
    if sys.platform == 'darwin':
        iconset = work/'Mobile.iconset'
        iconset.mkdir(exist_ok=True)
        for size in [16,32,128,256,512]:
            for scale in [1,2]:
                pixels = min(size*scale,512)
                name = f'icon_{size}x{size}'+('@2x' if scale==2 else '')+'.png'
                subprocess.run(['/usr/bin/sips','-z',str(size*scale),str(size*scale),str(root/'app.png'),
                                '--out',str(iconset/name)],check=True,capture_output=True)
        icon = work/'Mobile.icns'
        subprocess.run(['/usr/bin/iconutil','-c','icns',str(iconset),'-o',str(icon)],check=True)
        info = {'CFBundleDisplayName':'Codex 手机消息助手','CFBundleShortVersionString':'1.3.7',
                'CFBundleVersion':'11','LSMinimumSystemVersion':'14.0','LSUIElement':True}
        spec = common + f"""exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name='CodexRelayMobile',console=False,
          target_arch={platform.machine()!r},codesign_identity=None)
coll = COLLECT(exe,a.binaries,a.datas,name='CodexRelayMobile')
app = BUNDLE(coll,name='CodexRelayMobile.app',icon={str(icon)!r},bundle_identifier='local.codexrelay.mobile',info_plist={info!r})
"""
    else:
        spec = common + f"""exe = EXE(pyz,a.scripts,a.binaries,a.datas,[],name='CodexRelayMobile',console=False,
          icon={str(root/'app.ico')!r},upx=False)
"""
    spec_path = work/'CodexRelayMobile.spec'
    spec_path.write_text(spec,'utf-8')
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--distpath',str(destination),
                    '--workpath',str(work/'build'),str(spec_path)],check=True)


if __name__ == '__main__':
    main()
