import pathlib
import traceback

from PIL import Image
import numpy as np
import os

from src import core
from src import backbone
from src.common_constants import GenerationOptions as go
import subprocess

def open_path_as_images_generator(path, maybe_depthvide=False, batch_size=400, max_frames=None):
    suffix = pathlib.Path(path).suffix 
    if suffix.lower() in ['.webm', '.mp4', '.avi']:
        from moviepy.video.io.VideoFileClip import VideoFileClip
        clip = VideoFileClip(path)
        frames_batch=[]
        # frames = [Image.fromarray(x) for x in list(clip.iter_frames())]
        # TODO: Wrapping frames into Pillow objects is wasteful
        for cnt,frame in enumerate(clip.iter_frames()):
            # 将当前帧添加到批次列表中
            if max_frames is not None and cnt>=max_frames:
                break
            frames_batch.append(Image.fromarray(frame))
            # 检查是否达到批次大小
            if len(frames_batch) == batch_size:
                # 达到批次大小时，yield当前批次的帧，并重置批次列表
                yield clip.fps, frames_batch
                frames_batch = []
        # 处理完所有帧后，如果还有剩余的帧没有返回，则在这里返回
        if frames_batch:
            yield clip.fps, frames_batch
        #return clip.fps, frames
    else:
        raise NotImplementedError("only video is supported")

def open_path_as_images(path, maybe_depthvideo=False):
    """Takes the filepath, returns (fps, frames). Every frame is a Pillow Image object"""
    suffix = pathlib.Path(path).suffix
    if suffix.lower() == '.gif':
        frames = []
        img = Image.open(path)
        for i in range(img.n_frames):
            img.seek(i)
            frames.append(img.convert('RGB'))
        return 1000 / img.info['duration'], frames
    if suffix.lower() == '.mts':
        import imageio_ffmpeg
        import av
        container = av.open(path)
        frames = []
        for packet in container.demux(video=0):
            for frame in packet.decode():
                # Convert the frame to a NumPy array
                numpy_frame = frame.to_ndarray(format='rgb24')
                # Convert the NumPy array to a Pillow Image
                image = Image.fromarray(numpy_frame)
                frames.append(image)
        fps = float(container.streams.video[0].average_rate)
        container.close()
        return fps, frames
    if suffix.lower() in ['.avi'] and maybe_depthvideo:
        try:
            import imageio_ffmpeg
            # Suppose there are in fact 16 bits per pixel
            # If this is not the case, this is not a 16-bit depthvideo, so no need to process it this way
            gen = imageio_ffmpeg.read_frames(path, pix_fmt='gray16le', bits_per_pixel=16)
            video_info = next(gen)
            if video_info['pix_fmt'] == 'gray16le':
                width, height = video_info['size']
                frames = []
                for frame in gen:
                    # Not sure if this is implemented somewhere else
                    result = np.frombuffer(frame, dtype='uint16')
                    result.shape = (height, width)  # Why does it work? I don't remotely have any idea.
                    frames += [Image.fromarray(result)]
                    # TODO: Wrapping frames into Pillow objects is wasteful
                return video_info['fps'], frames
        finally:
            if 'gen' in locals():
                gen.close()
    if suffix.lower() in ['.webm', '.mp4', '.avi']:
        from moviepy.video.io.VideoFileClip import VideoFileClip
        clip = VideoFileClip(path)
        frames = [Image.fromarray(x) for x in list(clip.iter_frames())]
        # TODO: Wrapping frames into Pillow objects is wasteful
        return clip.fps, frames
    else:
        try:
            return 1, [Image.open(path)]
        except Exception as e:
            raise Exception(f"Probably an unsupported file format: {suffix}") from e


def frames_to_video(fps, frames, path, name, colorvids_bitrate=None):
    if frames[0].mode == 'I;16':  # depthmap video
        import imageio_ffmpeg
        writer = imageio_ffmpeg.write_frames(
            os.path.join(path, f"{name}.avi"), frames[0].size, 'gray16le', 'gray16le', fps, codec='ffv1',
            macro_block_size=1)
        try:
            writer.send(None)
            for frame in frames:
                writer.send(np.array(frame))
        finally:
            writer.close()
    else:
        arrs = [np.asarray(frame) for frame in frames]
        from moviepy.video.io.ImageSequenceClip import ImageSequenceClip
        clip = ImageSequenceClip(arrs, fps=fps)
        done = False
        #priority = [('avi', 'png'), ('avi', 'rawvideo'), ('mp4', 'libx264'), ('webm', 'libvpx')]
        priority = [('mp4', 'libx264', ["-c:v", "h264_nvenc"])],
        if colorvids_bitrate:
            priority = reversed(priority)
        #for v_format, codec, ffmpeg_params in priority:
        try:
            br =None # f'{colorvids_bitrate}k' #if codec not in ['png', 'rawvideo'] else None
            clip.write_videofile(os.path.join(path, f"{name}.mp4"), codec="libx264", bitrate=br)#, ffmpeg_params=["-c:v", "h264_nvenc"])
            done = True
        except:
            traceback.print_exc()
        #if not done:
            raise Exception('Saving the video failed!')


def process_predicitons(predictions, smoothening='none'):
    def global_scaling(objs, a=None, b=None):
        """Normalizes objs, but uses (a, b) instead of (minimum, maximum) value of objs, if supplied"""
        normalized = []
        min_value = a if a is not None else min([obj.min() for obj in objs])
        max_value = b if b is not None else max([obj.max() for obj in objs])
        for obj in objs:
            normalized += [(obj - min_value) / (max_value - min_value)]
        return normalized

    print('Processing generated depthmaps')
    # TODO: Detect cuts and process segments separately
    if smoothening == 'none':
        return global_scaling(predictions)
    elif smoothening == 'experimental':
        processed = []
        clip = lambda val: min(max(0, val), len(predictions) - 1)
        for i in range(len(predictions)):
            f = np.zeros_like(predictions[i])
            for u, mul in enumerate([0.10, 0.20, 0.40, 0.20, 0.10]):  # Eyeballed it, math person please fix this
                f += mul * predictions[clip(i + (u - 2))]
            processed += [f]
        # This could have been deterministic monte carlo... Oh well, this version is faster.
        a, b = np.percentile(np.stack(processed), [0.5, 99.5])
        return global_scaling(predictions, a, b)
    return predictions

def concat_videos(video_path, video_filenames, output_filename="output.mp4"):
    """
    使用FFmpeg无损地合并视频文件。
    
    参数:
    video_path (str): 视频文件所在的目录路径。
    video_filenames (list): 要合并的视频文件名列表。
    output_filename (str): 合并后视频文件的名称，默认为"output.mp4"。
    """
    print(f'video path is {video_path}')
    print(f'os.path.sep is {os.path.sep}')
    # 确保视频路径以斜杠结尾
    if not video_path.endswith(os.path.sep):
        video_path += os.path.sep

    # 创建文件列表内容
    filelist_content = "\n".join([f"file '{filename}'" for filename in video_filenames])
    filelist_path = os.path.join(video_path, 'filelist.txt')

    # 将文件列表内容写入临时txt文件
    with open(filelist_path, 'w') as filelist:
        filelist.write(filelist_content)

    # 构建FFmpeg命令
    ffmpeg_cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', filelist_path, '-c', 'copy', os.path.join(video_path, output_filename)]

    # 执行FFmpeg命令
    subprocess.run(ffmpeg_cmd)

    # 清理，删除文件列表
    os.remove(filelist_path)
    print(f"merge complete: {os.path.join(video_path, output_filename)}")


def gen_video(video, outpath, inp, custom_depthmap=None, colorvids_bitrate=None, smoothening='none'):
    if inp[go.GEN_SIMPLE_MESH.name.lower()] or inp[go.GEN_INPAINTED_MESH.name.lower()]:
        return 'Creating mesh-videos is not supported. Please split video into frames and use batch processing.'
    os.makedirs(backbone.get_outpath(), exist_ok=True)
    #fps, input_images = open_path_as_images(os.path.abspath(video.name))
    seq_no=0
    video_path_list = []
    for fps, input_images in open_path_as_images_generator(os.path.abspath(video.name), batch_size=100, max_frames=150):
        seq_no+=1
        if custom_depthmap is None:
            print('Generating depthmaps for the video frames')
            needed_keys = [go.COMPUTE_DEVICE, go.MODEL_TYPE, go.BOOST, go.NET_SIZE_MATCH, go.NET_WIDTH, go.NET_HEIGHT]
            needed_keys = [x.name.lower() for x in needed_keys]
            first_pass_inp = {k: v for (k, v) in inp.items() if k in needed_keys}
            # We need predictions where frames are not normalized separately.
            first_pass_inp[go.DO_OUTPUT_DEPTH_PREDICTION] = True
            # No need in normalized frames. Properly processed depth video will be created in the second pass
            first_pass_inp[go.DO_OUTPUT_DEPTH.name] = False

            gen_obj = core.core_generation_funnel(None, input_images, None, None, first_pass_inp)
            input_depths = [x[2] for x in list(gen_obj)]
            input_depths = process_predicitons(input_depths, smoothening)
        else:
            print('Using custom depthmap video')
            cdm_fps, input_depths = open_path_as_images(os.path.abspath(custom_depthmap.name), maybe_depthvideo=True)
            assert len(input_depths) == len(input_images), 'Custom depthmap video length does not match input video length'
            if input_depths[0].size != input_images[0].size:
                print('Warning! Input video size and depthmap video size are not the same!')

        print('Generating output frames')
        img_results = list(core.core_generation_funnel(None, input_images, input_depths, None, inp))
        gens = list(set(map(lambda x: x[1], img_results)))

        print('Saving generated frames as video outputs')
        for gen in gens:
            if gen == 'depth' and custom_depthmap is not None:
                # Well, that would be extra stupid, even if user has picked this option for some reason
                # (forgot to change the default?)
                continue

            imgs = [x[2] for x in img_results if x[1] == gen]
            basename = f'{gen}_video'
            video_file_name = f"depthmap-{backbone.get_next_sequence_number(outpath, basename)}-{basename}-{seq_no:04}"
            frames_to_video(fps, imgs, outpath, video_file_name,
                            colorvids_bitrate)
            video_path_list.append(f"{video_file_name}.mp4")
    concat_videos(outpath, video_path_list)

    for filename in video_path_list:
        file_to_delete = os.path.join(outpath, filename)
        try:
            os.remove(file_to_delete)
            print(f"deleted: {file_to_delete}")
        except OSError as e:
            print(f"error: {file_to_delete} can't delete. reason: {e}")

        
    print('All done. Video(s) saved!')
    return '<h3>Videos generated</h3>' if len(gens) > 1 else '<h3>Video generated</h3>' if len(gens) == 1 \
        else '<h3>Nothing generated - please check the settings and try again</h3>'
