import os
import re

reg6 = r".+?(?=/home)"
indir = '/home/guyf/DiariZen/recipes/diar_ssl/data/AMI_AliMeeting_AISHELL4/test/recording/wav_ori.scp'
filedir ='/data/guyf/data_dir/record'
oudir='/home/guyf/DiariZen/recipes/diar_ssl/data/AMI_AliMeeting_AISHELL4/test/recording/wav.scp'
#filedir = '/home/guyf/DiariZen/recipes/diar_ssl/data/AMI_AliMeeting_AISHELL4/wavs/test'
type_1 = True
cnt = 1
with open(indir, 'r', encoding='utf-8') as f:
    txt_b = f.readlines()
txt_c = ''.join(txt_b)
a = re.findall(reg6, txt_c)
e = []
for root, dirs, files in os.walk(filedir):  # 遍历
    for name in files:
        if name.endswith('Mix-Headset.wav'):
            os.rename(filedir + '/' + name, filedir + '/' + name[:-16] + '.wav')
#with open(indir, 'w', encoding='utf-8') as stone:  # 建立文件
for root, dirs, files in os.walk(filedir):  # 遍历
    for name in files:
        for i in range(len(a)):
            if name.startswith(a[i][:-1]) and name.endswith('.wav'):
                # 绝对路径
                # stone.write('examples' + bar_type + name + '\n')  # 写路径
                e.append(a[i] + filedir + '/' + name + '\n')
            #  stone.write(a[i] + filedir + '/' + name + '\n')  # 写路径
# 相对路径
e.sort()
# stone.write(
#      root.replace(filedir + '/', ('..' + '/' ) * cnt) + '/' + name + '\n')
with open(oudir, 'w', encoding='utf-8') as stone:
    if type_1:  # 绝对路径
        for i in range(len(e)):
            stone.write(e[i])  # 写路径
    else:  # 相对路径
        stone.write(
            root.replace(filedir + '/', ('..' + '/') * cnt) + '/' + name + '\n')

wavs = []
try:
    # input is wav list
    # with open('/home/guyf/3D-Speaker/3D-Speaker-main/egs/3dspeaker/speaker-diarization/examples/wav.list', 'r') as word_list:
    with open(oudir, 'r') as f:
        wav_list = f.readlines()
        print(wav_list)
except:
    raise Exception('Input should be a wav file or a wav list.')
