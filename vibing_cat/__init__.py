import os, stat
from madmom.features.beats import RNNBeatProcessor
import typer
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import shlex

def main():
    typer.run(process)

def process(audio_file: Path = typer.Argument(..., help='The audio file to beatmatch to'),
        video_file: Path = typer.Argument(..., help='The video file to composite on top of'),
        output_video_file: Path = typer.Argument(..., help='Where to output the resulting video file'),
        show_plots: bool = typer.Option(False, help='Whether to generate beat matching plots'),
        output_render_script: Path = typer.Option(Path('render.sh'), help='Path to output the render script to'),
        overlay_video_file: Path = typer.Option(Path('cat.mp4'), help='Path to the overlay video file'),
        intermediate_output_file: Path = typer.Option(Path('intermediate.mp4'), help='Path to use as the intermediate file for the compositing step'),
        offset: float = typer.Option(0, help='Add a constant offset to the video'),
        beat_threshold: float = typer.Option(0.5, help='The minimum certainty for a beat to be considered, between 0 and 1.  Increase this value if insufficient beats are found, and vice versa.'),
        short_outlier_cutoff: float = typer.Option(0.25, help='Number of standard deviations from the median that a beat is required to be considered a short pause'),
        long_outlier_cutoff: float = typer.Option(0.25, help='Number of standard deviations from the median that a beat is required to be considered a long pause'),
        beats_per_second: float = typer.Option(2, help='The number of beats per second in overlay_video_file'),
        frames_per_beat: int = typer.Option(15, help='The number of frames per beat in overlay_video_file'),
        n_beats: int = typer.Option(20, help='The number of beats present in overlay_video_file'),
        colorkey: str = typer.Option('0x2bd51b:0.15:0.15', help='The colorkey filter argument to be supplied to ffmpeg for compositing')):
    """
    Ouputs a render script in output_render_script to generated a composited, beat-matched vibing cat.
    """

    beats, beat_delays = analyse_audio(audio_file, short_outlier_cutoff,
        long_outlier_cutoff, beat_threshold, show_plots)
    beat_delays[0] = beat_delays[0] + offset
    command = construct_ffmpeg_arguments(overlay_video_file, audio_file,
            intermediate_output_file, beats_per_second, frames_per_beat, n_beats,
            beat_delays)
    command2 = chromakey(video_file, intermediate_output_file, colorkey,
            output_video_file)

    with open(output_render_script, 'w') as f:
        f.write(command)
        f.write('\n')
        f.write(command2)
    os.chmod(output_render_script, os.stat(output_render_script).st_mode | 
            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def analyse_audio(path, short_outlier_cutoff, long_outlier_cutoff,
        beat_threshold, show_plots):
    """ Analyse audio file at path, returning a list of bpm change timestamps
    short_outlier_cutoff: number of standard deviations from the median that 
            a beat is required to be considered a short pause
    long_outlier_cutoff: number of standard deviations from the median that 
            a beat is required to be considered a long pause
    """

    print('Processing beats')
    proc = RNNBeatProcessor()
    samples = proc(str(path))

    if show_plots:
        plt.scatter(list(map(lambda x:x/100, range(len(samples)))), samples)
        plt.xlabel('Time (s)')
        plt.ylabel('Beat certainty')
        plt.title('Certainty of each 10ms interval corresponding to a beat')
        plt.show()

    # Process samples into beats
    video_bpm = 120
    video_path = '../vibing_timed_transparent.mp4'
    
    # Calculate time since last beat
    print('Processing time since last beat')
    beats = []
    time_since_last_beat = []
    on_beat = False
    last_beat = 0
    for i, sample in enumerate(samples):
        if sample > beat_threshold and not on_beat:
            time_since_last_beat.append(i/100 - last_beat)
            last_beat = i/100
            beats.append(last_beat)
            on_beat = True
        elif sample <= beat_threshold:
            on_beat = False
    
    # Separate outliers, dumping short
    print('Separating outliers')
    beat_median = np.median(time_since_last_beat)
    beat_std = np.std(time_since_last_beat)
    lower = beat_median - (short_outlier_cutoff * beat_std)
    upper = beat_median + (long_outlier_cutoff * beat_std)
    print(f'median: {beat_median}')
    print(f'beat_std: {beat_std}')
    print(f'lower: {lower}')
    print(f'upper: {upper}')

    time_since_last_beat_outliers = []
    for beat in time_since_last_beat:
        if beat > upper:
            time_since_last_beat_outliers.append((beat, True))
        else:
            time_since_last_beat_outliers.append((beat, False))

    if show_plots:
        plt.axhline(y=beat_median, color='g', label='median')
        plt.axhline(y=upper, color='r', label='lower_threshold')
        plt.axhline(y=lower, color='r', label='upper_threshold')

        plt.scatter(range(len(time_since_last_beat)), time_since_last_beat)

        plt.legend(bbox_to_anchor = (1.0, 1), loc = 'upper center')
        plt.xlabel('Beat')
        plt.ylabel('Time since last beat')
        plt.title('Time since last beat, for each beat, showing outlier cutoffs')
        plt.show()

    # For all long pauses, forward fill between bpms
    print('forward filling pauses')
    new_beats = []
    in_pause = False
    for i, (b, outlier) in enumerate(time_since_last_beat_outliers):
        if not outlier:
            if in_pause:
                in_pause=False
            if not new_beats:
                new_beats.append(b)
            else:
                new_beats.append(new_beats[-1] + b)
        else:
            # Skip if in a pause that's already been processed
            if not in_pause:
                in_pause = True
                # If no beat has been output, output one and carry on
                if not new_beats:
                    new_beats.append(b)
                    continue

                # Seek forward to find next good beat
                (last_good_beat_time, _) = time_since_last_beat_outliers[i-1]
                next_outliers = []
                for (b1, outlier1) in time_since_last_beat_outliers[i+1:]:
                    if outlier1:
                        next_outliers.append(b1)
                    else:
                        next_good_beat_time = b1
                        break
                time_to_fill = b + sum(next_outliers)

                # Naive approach: linear scale between last and next good beat
                avg_beat_time = (last_good_beat_time + next_good_beat_time)/2
                beats_in_time = round(time_to_fill/avg_beat_time)
                naive_beat_length = time_to_fill / beats_in_time
                #beat_offset = (next_good_beat_time - last_good_beat_time) / beats_in_time

                for beat in range(beats_in_time):
                    new_beats.append(new_beats[-1] + naive_beat_length) # TODO: scale linearly

    if show_plots:
        plt.scatter(beats, [0]*len(beats))
        plt.scatter(new_beats, [0.1]*len(new_beats))
        plt.scatter([0], [1])
        plt.xlabel('Time (s)')
        plt.title('Beat locations - original (bottom) vs filled (top)')
        plt.show()

    # Calculate bpm
    diffs = []
    for i, j in zip(beats, beats[1:]):
        diffs.append(j-i)

    diffs.sort()

    quarter = int(len(diffs)/4)
    mid_diffs = diffs[quarter:3*quarter]
    average = sum(mid_diffs)/len(mid_diffs)
    bpm = (1/average)*60
    print(f'average bpm: {bpm}')

    playback_factor = round(bpm)/video_bpm

    # Calculate time since last beat
    last_beat = 0
    beat_delays = []
    for beat in new_beats:
        beat_delays.append(beat - last_beat)
        last_beat = beat

    return new_beats, beat_delays

def construct_ffmpeg_arguments(input_video, input_audio, output_video,
        beats_per_second, frames_per_beat, n_beats, beat_delays):
    input_video = shlex.quote(str(input_video))
    input_audio = shlex.quote(str(input_audio))
    output_video = shlex.quote(str(output_video))
    command = f'ffmpeg -y -i {input_video} -i {input_audio} -filter_complex \\\n"'

    for i, beat_length in enumerate(beat_delays):
        beat = i % n_beats
        start_frame = frames_per_beat * beat
        end_frame = frames_per_beat * (beat + 1)
        scale_factor = beat_length * beats_per_second
        command += f'[0:v]trim=start_frame={start_frame}:end_frame={end_frame},setpts=(PTS-STARTPTS)*{scale_factor}[v{i}]; \\\n'

    for i in range(len(beat_delays)):
        command += f'[v{i}]'

    concat_n = i+1
    command += f'concat=n={concat_n}:v=1[new]" -c:a aac -map "[new]" -map 1:a:0 {output_video}'

    return command

def chromakey(input_video, overlay_video, colorkey, output_video):
    input_video = shlex.quote(str(input_video))
    overlay_video = shlex.quote(str(overlay_video))
    output_video = shlex.quote(str(output_video))

    return f'ffmpeg -y -i {input_video} -i {overlay_video} -filter_complex "[1:v]colorkey={colorkey}[ckout];[0:v][ckout]overlay=0:main_h-overlay_h[despill];[despill] despill=green[colorspace];[colorspace]format=yuv420p[out]" -map "[out]" -map 1:a:0 {output_video}'

