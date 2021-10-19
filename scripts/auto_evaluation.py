import rospy
import rospkg
import subprocess
import os
import argparse
import time
import xml.etree.ElementTree as ET


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('-test-bags', '--test-rosbag-files', required=True, type=str, nargs='+', help=".bag files to test on")
    parser.add_argument('-gt-bags', '--gt-rosbag-files', required=True, type=str, nargs='+', help=".bag files with gt poses")
    parser.add_argument('-gt-topic', '--gt-topic', required=True, type=str, help="topic to read gt poses")
    parser.add_argument('-out-test-fld', '--out-test-folder', required=True, type=str)
    parser.add_argument('-val-fld', '--validation-folder', required=True, type=str)

    parser.add_argument('-robot', '--robot-name', type=str, default='default')
    parser.add_argument('-dim', '--dimension', type=str, default='3d', choices=['3d', '2d'], help="Which SLAM to use: 2d or 3d.")
    parser.add_argument('-node', '--node-to-use', type=str, default='online', choices=['online', 'offline'], help="Cartographer mode.")
    parser.add_argument('--get-odom-from-transforms', action='store_true')
    parser.add_argument('--get-odom-from-topic', action='store_true')
    parser.add_argument('--test-name', type=str, default='test')

    parser.add_argument('--max-union-intersection-time-difference', type=float, default=0.9, help="Max difference between union and intersection or time ragnes where gt and SLAM poses are set.")
    parser.add_argument('--max-time-error', type=float, default=0.01, help="Max time error during matching gt and SLAM poses.")
    parser.add_argument('--max-time-step', type=float, default=0.7, help="Max time step in gt and SLAM poses after matching.")

    parser.add_argument('--skip-running-cartographer', action='store_true')
    parser.add_argument('--skip-trajectory-extraction', action='store_true')
    parser.add_argument('--skip-poses-preparation', action='store_true')
    parser.add_argument('--skip-evaluation', action='store_true')
    return parser


def make_dirs(out_test_folder, validation_folder):
    os.makedirs(out_test_folder, exist_ok=True)
    os.makedirs(os.path.join(validation_folder, 'gt'), exist_ok=True)
    os.makedirs(os.path.join(validation_folder, 'results'), exist_ok=True)


def robot_name_to_imu_frame(robot: str):
    # imu frame does not depend on dim, but this variable is needed to evaluate expression from cartographer.launch file
    dim = '3d'
    # find cartographer.launch file and open it with xml parser
    rospack = rospkg.RosPack()
    cartographer_example_folder = rospack.get_path('cartographer_example')
    tree = ET.parse(os.path.join(cartographer_example_folder, 'launch/cartographer.launch'))
    root = tree.getroot()
    for elem in root:
        # find argument named config_file
        if elem.tag != 'arg':
            continue
        if elem.attrib['name'] != 'config_file':
            continue
        # evaluate expression
        expression = elem.attrib.get('if')
        if expression is None:
            continue
        expression = expression.replace('$(eval', '')
        expression = expression[:expression.rfind(')')]
        if not eval(expression):
            continue
        # find config file path and read it
        config_filename = os.path.join(cartographer_example_folder, 'config', elem.attrib['value'])
        with open(config_filename, 'r') as f:
            lines = f.readlines()
        # find tracking_frame variable and read its value
        imu_frame = None
        for num, line in enumerate(lines):
            if line.find('tracking_frame') == -1:
                continue
            start_idx = line.find('=')
            if start_idx == -1 or start_idx == len(line) - 1:
                raise RuntimeError
            start_idx += 1
            end_idx = len(line)
            imu_frame = eval(line[start_idx:end_idx])
            if isinstance(imu_frame, tuple):
                imu_frame = imu_frame[0]
            if not isinstance(imu_frame, str):
                raise RuntimeError
            break
        if imu_frame is None:
            raise RuntimeError
        # check that tracking_frame variable encounters only once
        for i in range(num+1, len(lines)):
            if lines[i].find('tracking_frame') != -1:
                raise RuntimeError
        return imu_frame
    raise RuntimeError


def start_reading_cartographer_odometry(robot_name, out_results_rosbag_filename, get_odom_from_transforms=False, get_odom_from_topic=False):
    if get_odom_from_transforms:
        imu_frame = robot_name_to_imu_frame(robot_name)
        command = "rosrun ros_utils read_transforms.py    -from odom    -to {}    \
-out-bag {}    -out-topic {}".format(imu_frame, out_results_rosbag_filename, 'local_trajectory_0')
    elif get_odom_from_topic:
        command = "rosbag record local_trajectory_0 -O {} local_trajectory_0:=/cartographer/tracked_pose".format(out_results_rosbag_filename)
    else:
        raise RuntimeError()
    cartographer_odometry_reader = subprocess.Popen(command.split())
    return cartographer_odometry_reader


def stop_reading_cartographer_odometry(cartographer_odometry_reader: subprocess.Popen):
    cartographer_odometry_reader.terminate()
    cartographer_odometry_reader.communicate()


def run_cartographer(rosbag_filenames, out_pbstream_filename, robot_name='default', dimension='3d', \
                     node_to_use='online', print_log=False):
    if isinstance(rosbag_filenames, str):
        rosbag_filenames = [rosbag_filenames]
    if node_to_use not in ['online', 'offline']:
        raise RuntimeError()
    if node_to_use == 'offline':
        raise NotImplementedError('Launch file for offline node is out of date.')

    if node_to_use == 'online':
        command = "roslaunch cartographer_example cartographer.launch   robot:={}    dim:={}    \
publish_occupancy_grid:=false".format(robot_name, dimension)
    if print_log:
        print('\n\n\n\n' + command + '\n')
    process = subprocess.Popen(command.split())
    
    if node_to_use == 'online':
        rospy.wait_for_service('/cartographer/write_state')
        time.sleep(3)
        rosbag_play_command = "rosbag play --clock {}".format(' '.join(rosbag_filenames))
        rosbag_play = subprocess.Popen(rosbag_play_command.split())
        rosbag_play.communicate()
        assert(rosbag_play.returncode == 0)
        time.sleep(1)
        
        finish_trajectory_command = "rosservice call /cartographer/finish_trajectory 0"
        finish_trajectory = subprocess.Popen(finish_trajectory_command.split())
        finish_trajectory.communicate()
        assert(finish_trajectory.returncode == 0)
        
        trajectory_state = 0
        while trajectory_state == 0:
            get_trajectory_states_command = "rosservice call /cartographer/get_trajectory_states"
            trajectory_states = subprocess.check_output(get_trajectory_states_command.split()).decode()
            idx = trajectory_states.find('trajectory_state:')
            idx += trajectory_states[idx:].find('[')
            trajectory_state = int(trajectory_states[idx+1:idx+2])
        time.sleep(1)
        
        write_state_command = "rosservice call /cartographer/write_state {} true".format(out_pbstream_filename)
        write_state = subprocess.Popen(write_state_command.split())
        write_state.communicate()
        assert(write_state.returncode == 0)
        process.terminate()

    process.communicate()
    assert(process.returncode == 0)
    return command


def extract_SLAM_trajectories(pbstream_filename, out_results_rosbag_filename, robot_name='default', print_log=False):
    imu_frame = robot_name_to_imu_frame(robot_name)
    command = "rosrun cartographer_ros pbstream_trajectories_to_rosbag    -input {}    -output {}    \
-tracking_frame {}".format(pbstream_filename, out_results_rosbag_filename, imu_frame)
    if print_log:
        print('\n\n\n\n' + 'imu frame: ' + imu_frame + '\n')
        print(command + '\n')
    process = subprocess.Popen(command.split())
    process.communicate()
    assert(process.returncode == 0)
    return command


def robot_name_to_transforms_source_filename(robot: str):
    # find cartographer.launch file and open it with xml parser
    rospack = rospkg.RosPack()
    cartographer_example_folder = rospack.get_path('cartographer_example')
    tree = ET.parse(os.path.join(cartographer_example_folder, 'launch/cartographer.launch'))
    root = tree.getroot()
    for elem in root:
        # find include tag
        if elem.tag != 'include':
            continue
        # evaluate expression
        expression = elem.attrib.get('if')
        if expression is None:
            continue
        expression = expression.replace('$(eval', '')
        expression = expression[:expression.rfind(')')]
        if not eval(expression):
            continue
        # find transforms source file path
        transforms_source_file = elem.attrib['file'].replace('$(find cartographer_example)/', '')
        transforms_source_filename = os.path.join(cartographer_example_folder, transforms_source_file)
        return transforms_source_filename
    raise RuntimeError


def prepare_poses_for_evaluation(gt_rosbag_filenames, gt_topic, results_rosbag_filenames, results_topic, \
                                 out_gt_poses_filename, out_results_poses_filename, robot_name='default', \
                                 out_trajectories_rosbag_filename='\'\'', max_union_intersection_time_difference=0.9, \
                                 max_time_error=0.01, max_time_step=0.7, print_log=False):
    if isinstance(gt_rosbag_filenames, str):
        gt_rosbag_filenames = [gt_rosbag_filenames]
    if isinstance(results_rosbag_filenames, str):
        results_rosbag_filenames = [results_rosbag_filenames]
    transforms_source_filename = robot_name_to_transforms_source_filename(robot_name)
    command = "rosrun ros_utils prepare_poses_for_evaluation.py    \
-gt-bags {}    -gt-topic {}    -res-bag {}    -res-topic {}     -out-gt {}    -out-res {}    \
-transforms-source {}    -out-trajectories {}    --max-union-intersection-time-difference {}    --max-time-error {}    \
--max-time-step {}".format(' '.join(gt_rosbag_filenames), gt_topic, ' '.join(results_rosbag_filenames), results_topic, out_gt_poses_filename, \
                           out_results_poses_filename, transforms_source_filename, out_trajectories_rosbag_filename, \
                           max_union_intersection_time_difference, max_time_error, max_time_step)
    if print_log:
        print('\n\n\n\n' + 'transforms source filename: ' + transforms_source_filename + '\n')
        print(command + '\n')
    process = subprocess.Popen(command.split())
    process.communicate()
    assert(process.returncode == 0)
    return command


def run_evaluation(validation_folder, projection='xy', print_log=False):
    gt_poses_folder = os.path.abspath(os.path.join(validation_folder, 'gt'))
    results_poses_folder = os.path.abspath(os.path.join(validation_folder, 'results'))
    out_folder = os.path.abspath(os.path.join(validation_folder, 'output_{}'.format(projection)))

    command = "python3 /home/cds-jetson-host/slam_validation/evaluate_poses.py    --dir_gt {}    --dir_result {}    \
--dir_output {}    --gt_format kitti    --result_format kitti    \
--projection {}".format(gt_poses_folder, results_poses_folder, out_folder, projection)
    if print_log:
        print('\n\n\n\n' + command + '\n')
    process = subprocess.Popen(command.split())
    process.communicate()
    assert(process.returncode == 0)
    return command


def auto_evaluation(test_rosbag_files, gt_rosbag_files, gt_topic, out_test_folder, validation_folder, \
                    robot_name='default', dimension='3d', node_to_use='online', \
                    get_odom_from_transforms=False, get_odom_from_topic=False, test_name='test', \
                    max_union_intersection_time_difference=0.9, max_time_error=0.01, max_time_step=0.7, \
                    skip_running_cartographer=False, skip_trajectory_extraction=False, skip_poses_preparation=False, skip_evaluation=False):
    if isinstance(test_rosbag_files, str):
        test_rosbag_files = [test_rosbag_files]
    if isinstance(gt_rosbag_files, str):
        gt_rosbag_files = [gt_rosbag_files]
    make_dirs(out_test_folder, validation_folder)
    log = str()

    # Run cartographer to generate a map
    test_rosbag_filenames = list(map(os.path.abspath, test_rosbag_files))
    out_pbstream_filename = os.path.abspath(os.path.join(out_test_folder, '{}.pbstream'.format(test_name)))
    out_results_rosbag_filename = os.path.abspath(os.path.join(out_test_folder, '{}.bag'.format(test_name)))
    poses_from_cartographer_map = not (get_odom_from_transforms or get_odom_from_topic)
    if not skip_running_cartographer:
        if not poses_from_cartographer_map:
            cartographer_odometry_reader = start_reading_cartographer_odometry(robot_name, out_results_rosbag_filename, \
                                                                               get_odom_from_transforms=get_odom_from_transforms, \
                                                                               get_odom_from_topic=get_odom_from_topic)
        command = run_cartographer(test_rosbag_filenames, out_pbstream_filename, robot_name=robot_name, dimension=dimension, \
                                   node_to_use=node_to_use, print_log=True)
        if not poses_from_cartographer_map:
            stop_reading_cartographer_odometry(cartographer_odometry_reader)
        log += command + '\n\n\n\n'
    
    # Extract SLAM trajectories from cartographer map
    if not skip_trajectory_extraction and poses_from_cartographer_map:
        command = extract_SLAM_trajectories(out_pbstream_filename, out_results_rosbag_filename, robot_name, print_log=True)
        log += command + '\n\n\n\n'

    # Prepare poses in kitti format for evaluation
    gt_rosbag_filenames = list(map(os.path.abspath, gt_rosbag_files))
    out_global_gt_poses_filename = os.path.abspath(os.path.join(validation_folder, 'gt', 'global_{}.txt'.format(test_name)))
    out_global_results_poses_filename = os.path.abspath(os.path.join(validation_folder, 'results', 'global_{}.txt'.format(test_name)))
    out_local_gt_poses_filename = os.path.abspath(os.path.join(validation_folder, 'gt', 'local_{}.txt'.format(test_name)))
    out_local_results_poses_filename = os.path.abspath(os.path.join(validation_folder, 'results', 'local_{}.txt'.format(test_name)))
    out_trajectories_rosbag_filename = os.path.abspath(os.path.join(out_test_folder, '{}_trajectories.bag'.format(test_name)))
    if not skip_poses_preparation:
        if poses_from_cartographer_map:
            command = prepare_poses_for_evaluation(gt_rosbag_filenames, gt_topic, out_results_rosbag_filename, 'global_trajectory_0', \
                                                out_global_gt_poses_filename, out_global_results_poses_filename, \
                                                robot_name, out_trajectories_rosbag_filename, \
                                                max_union_intersection_time_difference=max_union_intersection_time_difference, \
                                                max_time_error=max_time_error, max_time_step=max_time_step, print_log=True)
            log += command + '\n\n\n\n'
        command = prepare_poses_for_evaluation(gt_rosbag_filenames, gt_topic, out_results_rosbag_filename, 'local_trajectory_0', \
                                               out_local_gt_poses_filename, out_local_results_poses_filename, \
                                               robot_name, out_trajectories_rosbag_filename, \
                                               max_union_intersection_time_difference=max_union_intersection_time_difference, \
                                               max_time_error=max_time_error, max_time_step=max_time_step, print_log=True)
        log += command + '\n\n\n\n'
    
    # Run evaluation
    if not skip_evaluation:
        for projection in ['xy', 'xz', 'yz']:
            command = run_evaluation(validation_folder, projection=projection, print_log=True)
            log += command + '\n\n\n\n'
    
    # print(log)


if __name__ == '__main__':
    parser = build_parser()
    args = parser.parse_args()
    auto_evaluation(**vars(args))
