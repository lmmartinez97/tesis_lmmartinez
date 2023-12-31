#!/usr/bin/env python

# Copyright (c) 2018 Intel Labs.
# authors: German Ros (german.ros@intel.com)
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

"""CARLA MCTS IMPLEMENTATION"""

from __future__ import print_function


import argparse
import asyncio
import glob
import json
import logging
import numpy as np
import os
import pandas as pd
import re
import sys
import traceback

from rich import print
from numpy import random


try:
    import pygame
    from pygame.locals import KMOD_CTRL
    from pygame.locals import K_ESCAPE
    from pygame.locals import K_q
except ImportError:
    raise RuntimeError("cannot import pygame, make sure pygame package is installed")

# ==============================================================================
# -- Find CARLA module ---------------------------------------------------------
# ==============================================================================
try:
    sys.path.append(
        glob.glob(
            "/home/lmmartinez/CARLA/PythonAPI/carla/dist/carla-*%d.%d-%s.egg"
            % (
                sys.version_info.major,
                sys.version_info.minor,
                "win-amd64" if os.name == "nt" else "linux-x86_64",
            )
        )[0]
    )
except IndexError:
    pass
import carla
from carla import ColorConverter as cc

from modules.camera import CameraManager, StaticCamera
from modules.hud import HUD, get_actor_display_name
from modules.keyboard_control import KeyboardControl
from modules.logger import DataLogger
from modules.printers import print_blue, print_green, print_highlight, print_red
from modules.sensors import CollisionSensor, GnssSensor, LaneInvasionSensor
from modules.shared_mem import SharedMemory
from modules.utils import get_straight_angle

from agents.navigation.basic_agent import BasicAgent  # pylint: disable=import-error
from agents.navigation.behavior_agent import BehaviorAgent  # pylint: disable=import-error
from agents.navigation.constant_velocity_agent import (ConstantVelocityAgent)  # pylint: disable=import-error

# ==============================================================================
# -- Global functions ----------------------------------------------------------
# ==============================================================================

log_host = "127.0.0.1"  # Replace with your server's IP address
log_port = 8888  # Replace with your server's port

# ==============================================================================
# -- Global functions ----------------------------------------------------------
# ==============================================================================


def find_weather_presets():
    """Method to find weather presets"""
    rgx = re.compile(".+?(?:(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|$)")

    def name(x):
        return " ".join(m.group(0) for m in rgx.finditer(x))

    presets = [x for x in dir(carla.WeatherParameters) if re.match("[A-Z].+", x)]
    return [(getattr(carla.WeatherParameters, x), name(x)) for x in presets]


# ==============================================================================
# -- Destination reached exception ---------------------------------------------
# ==============================================================================


# ==============================================================================
# -- World ---------------------------------------------------------------------
# ==============================================================================


class World(object):
    """Class representing the surrounding environment"""

    def __init__(self, carla_world, hud, args):
        """Constructor method"""
        self._args = args
        self.world = carla_world
        self.delta_simulated = 0.05

        try:
            self.map = self.world.get_map()
        except RuntimeError as error:
            print("RuntimeError: {}".format(error))
            print("The server could not send the OpenDRIVE (.xodr) file:")
            print(
                "Make sure it exists, has the same name of your town, and is correct."
            )
            sys.exit(1)

        self.hud = hud
        self.actor_list = []
        self.sensor_list = []
        self.player = None
        self.collision_sensor = None
        self.lane_invasion_sensor = None
        self.gnss_sensor = None
        self.camera_manager = None
        self.static_camera = None
        self._weather_presets = find_weather_presets()
        self._weather_index = 0
        self._actor_filter = args.filter
        self.world.on_tick(hud.on_world_tick)
        self.recording_enabled = False
        self.recording_start = 0
        self.blueprint_toyota_prius = None

        self.dataframe = pd.DataFrame()

        self.spawn_point_ego = carla.Transform(
            carla.Location(x=-850, y=-65, z=0.5),
            carla.Rotation(yaw=0, pitch=0, roll=0)
            )
        self.destination = carla.Location(x= 700, y= -61.5, z=0.5)

        self.traj_angle = get_straight_angle(
            (self.destination.x, self.destination.y), (self.spawn_point_ego.location.x, self.spawn_point_ego.location.y)
        )

        settings = self.world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = self.delta_simulated
        settings.no_rendering_mode = True
        settings.substepping = True
        settings.max_substep_delta_time = 0.01
        settings.max_substeps = int(self.delta_simulated * 50)
        self.world.apply_settings(settings)
        self.restart(args)

        print("World created")

    def restart(self, args):
        """Restart the world"""
        # Keep same camera config if the camera manager exists.
        cam_index = self.camera_manager.index if self.camera_manager is not None else 0
        cam_pos_id = (
            self.camera_manager.transform_index
            if self.camera_manager is not None
            else 0
        )

        # Get a random blueprint.
        self.blueprint_toyota_prius = random.choice(
            self.world.get_blueprint_library().filter("vehicle.toyota.prius")
        )
        self.blueprint_toyota_prius.set_attribute("role_name", "hero")
        self.blueprint_toyota_prius.set_attribute("color", "(11,166,86)")

        if not self.map.get_spawn_points():
            print("There are no spawn points available in your map/town.")
            print("Please add some Vehicle Spawn Point to your UE4 scene.")
            sys.exit(1)

        # Spawn the player.
        self.player = self.world.try_spawn_actor(
            self.blueprint_toyota_prius, self.spawn_point_ego
        )
        self.modify_vehicle_physics(self.player)
        print("Spawned ego vehicle")
        self.world.tick()

        # Set up the sensors.
        self.collision_sensor = CollisionSensor(self.player, self.hud)
        self.lane_invasion_sensor = LaneInvasionSensor(self.player, self.hud)
        self.gnss_sensor = GnssSensor(self.player)

        self.camera_manager = CameraManager(self.player, self.hud)
        self.camera_manager.transform_index = cam_pos_id
        self.camera_manager.set_sensor(cam_index, notify=False)

        self.actor_list.append(self.player)
        self.sensor_list.extend(
            [
                self.collision_sensor.sensor,
                self.lane_invasion_sensor.sensor,
                self.gnss_sensor.sensor,
                self.camera_manager.sensor,
            ]
        )

        # self.static_camera = StaticCamera(
        #     carla.Transform(
        #         carla.Location(x=-475, y=-67, z=100),
        #         carla.Rotation(yaw=0, pitch=-75, roll=0),
        #     ),
        #     self.world,
        #     args.width,
        #     args.height,
        # )
        # self.static_camera.set_sensor()

        actor_type = get_actor_display_name(self.player)
        self.hud.notification(actor_type)

    def record_frame_state(self, frame_number):
        """
        Saves the state of all vehicles in the world at the specified frame number.

        Args:
            frame_number (int): The frame number when the state is recorded.
        """
        local_df = pd.DataFrame()
        for vehicle in self.world.get_actors().filter("vehicle.*"):
            position = vehicle.get_location()
            rotation = vehicle.get_transform().rotation
            velocity = vehicle.get_velocity()
            ang_velocity = vehicle.get_angular_velocity()
            bounding_box = vehicle.bounding_box
            state_dict = {
                "id": vehicle.id,
                "frame": frame_number,
                "x": position.x,
                "y": position.y,
                "z": position.z,
                "pitch": rotation.pitch,
                "yaw": rotation.yaw,
                "roll": rotation.roll,
                "xVelocity": velocity.x,
                "yVelocity": velocity.y,
                "zVelocity": velocity.z,
                "xAngVelocity": ang_velocity.x,
                "yAngVelocity": ang_velocity.y,
                "zAngVelocity": ang_velocity.z,
                "width": 2 * bounding_box.extent.x,
                "height": 2 * bounding_box.extent.y,
            }
        local_df = pd.concat([local_df, pd.DataFrame([state_dict])], ignore_index=True)
        self.dataframe = pd.concat([self.dataframe, local_df])
        return local_df

    def restore_frame_state(self, frame_number):
        """
        Restores the state of all vehicles in the world to the specified frame.

        Args:
            frame_number (int): The frame number to restore the state.
        """
        groups = self.dataframe.groupby("frame")
        frame_df = groups.get_group(frame_number)
        print(frame_df)
        for index, row in frame_df.iterrows():
            actor = self.world.get_actor(int(row["id"]))
            actor.set_transform(
                carla.Transform(
                    carla.Location(x=row["x"], y=row["y"], z=row["z"]),
                    carla.Rotation(
                        pitch=row["pitch"], yaw=row["yaw"], roll=row["roll"]
                    ),
                )
            )
            actor.set_target_velocity(
                carla.Vector3D(
                    x=row["xVelocity"], y=row["yVelocity"], z=row["zVelocity"]
                )
            )
            actor.set_target_angular_velocity(
                carla.Vector3D(
                    x=row["xAngVelocity"],
                    y=row["yAngVelocity"],
                    z=row["zAngVelocity"],
                )
            )

    def next_weather(self, reverse=False):
        """Get next weather setting"""
        self._weather_index += -1 if reverse else 1
        self._weather_index %= len(self._weather_presets)
        preset = self._weather_presets[self._weather_index]
        self.hud.notification("Weather: %s" % preset[1])
        self.player.get_world().set_weather(preset[0])

    def modify_vehicle_physics(self, actor):
        # If actor is not a vehicle, we cannot use the physics control
        try:
            physics_control = actor.get_physics_control()
            physics_control.use_sweep_wheel_collision = True
            actor.apply_physics_control(physics_control)
        except Exception:
            pass

    def tick(self, clock, episode_number, frame_number):
        """Method for every tick"""
        self.hud.tick(self, clock, episode_number, frame_number)

    def render(self, display):
        """Render world"""
        self.camera_manager.render(display)
        self.hud.render(display)
        # self.static_camera.render(display)

    def destroy_sensors(self):
        """Destroy sensors"""
        self.camera_manager.sensor.destroy()
        self.camera_manager.sensor = None
        self.camera_manager.index = None

    def destroy(self):
        """Destroys all actors"""

        [sensor.destroy() for sensor in self.sensor_list]
        [actor.destroy() for actor in self.actor_list]
        self.actor_list.clear()
        self.sensor_list.clear()

        # sensors = list(self.world.get_actors().filter("sensor.*"))
        # [print(sensor.type_id, sensor.id) for sensor in sensors if sensor is not None]
        # print("_" * 20)
        # [sensor.destroy() for sensor in sensors]
        # actors = list(self.world.get_actors().filter("vehicle.*"))
        # [print(actor.type_id, actor.id) for actor in actors]
        # [actor.destroy() for actor in actors if actor is not None]

        # self.players.clear()
        print("Finished destroying actors")


def init_sim(args):
    pygame.init()
    pygame.font.init()

    client = carla.Client(args.host, args.port)
    client.set_timeout(6.0)
    client.load_world('mergin_scene_1')

    # get traffic manager
    traffic_manager = client.get_trafficmanager()
    sim_world = client.get_world()
    # apply settings
    settings = sim_world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = 0.05
    sim_world.apply_settings(settings)
    traffic_manager.set_synchronous_mode(True)

    # initialize display
    display = pygame.display.set_mode(
        (args.width, args.height), pygame.HWSURFACE | pygame.DOUBLEBUF
    )
    print("Created display")

    # initialize hud and world
    hud = HUD(args.width, args.height, text=__doc__)
    print("Created hud")
    world = World(sim_world, hud, args)
    print("Created world instance")

    controller = KeyboardControl(world)

    # initialize agent
    if args.agent == "Basic":
        agent = BasicAgent(world.player, 30)
        agent.follow_speed_limits(True)
    elif args.agent == "Constant":
        agent = ConstantVelocityAgent(world.player, 30)
        ground_loc = world.world.ground_projection(world.player.get_location(), 5)
        if ground_loc:
            world.player.set_location(ground_loc.location + carla.Location(z=0.01))
        agent.follow_speed_limits(True)
    elif args.agent == "Behavior":
        agent = BehaviorAgent(world.player, behavior=args.behavior)

    clock = pygame.time.Clock()

    return world, agent, clock, controller, display

def init_episode(world=None, clock = None, agent=None):

    # Set the agent destination
    destination = carla.Location(x = world.spawn_point_ego.location.x + 100*np.cos(np.pi - world.traj_angle),
                                 y = world.spawn_point_ego.location.y + 100*np.sin(np.pi - world.traj_angle), 
                                 z = world.spawn_point_ego.location.z) 
    route = agent.set_destination(destination)
    print("Spawn point is: ", world.spawn_point_ego.location)
    print("Destination is: ", destination)
    init_waypoint = route[0][0]
    init_location = world.player.get_location()
    world.player.set_transform(
        carla.Transform(
            carla.Location(x=init_location.x, y=init_location.y, z=init_location.z),
            carla.Rotation(
                pitch=init_waypoint.transform.rotation.pitch, yaw=init_waypoint.transform.rotation.yaw, roll=init_waypoint.transform.rotation.roll
            ),
        )
    )
    clock.tick()
    world.world.tick()
    world.dataframe = pd.DataFrame()

    input("Press Enter to start episode")
    prev_timestamp = world.world.get_snapshot().timestamp

    for waypoint, _ in route:
        world.world.debug.draw_point(
            waypoint.transform.location, size=0.1, color=carla.Color(255, 0, 0), life_time=100
        )

    return prev_timestamp

async def send_log_data(host, port, log_data):
    try:
        reader, writer = await asyncio.open_connection(host, port)
        data = json.dumps(log_data.to_dict(orient='records')).encode()
        writer.write(data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    except ConnectionRefusedError:
        pass
    
    except Exception as e:
        template = "An exception of type {0} occurred. Arguments:\n{1!r}"
        message = template.format(type(e).__name__, e.args)
        print(message)


async def game_step(
    episode_counter=-1,
    frame_counter=-1,
    world=None,
    agent=None,
    clock=None,
    prev_timestamp=None,
    display = None,
    controller = None
):
    clock.tick()
    timestamp = world.world.get_snapshot().timestamp
    frame_df = world.record_frame_state(frame_counter)
    world.world.tick()
    world.tick(clock, episode_counter, frame_counter)
    
    if display:
        world.render(display)
        pygame.display.flip()

    if controller and controller.parse_events():
        return -1, -1, prev_timestamp

    control = agent.run_step()
    control.manual_gear_shift = False
    world.player.apply_control(control)
    await send_log_data(log_host, log_port, frame_df)
    prev_timestamp = timestamp

    if agent.done():
        print("The target has been reached, restarting from spawn point")
        ret_step = -1
        ret_episode = 0
        if episode_counter > 10:
            ret_episode = -1
    else:
        ret_episode = 0
        ret_step = 0

    return ret_episode, ret_step, prev_timestamp


# ==============================================================================
# -- main() --------------------------------------------------------------
# ==============================================================================


async def main():
    """Main method"""

    argparser = argparse.ArgumentParser(description="CARLA Automatic Control Client")
    argparser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        dest="debug",
        help="Print debug information",
    )
    argparser.add_argument(
        "--host",
        metavar="H",
        default="127.0.0.1",
        help="IP of the host server (default: 127.0.0.1)",
    )
    argparser.add_argument(
        "-p",
        "--port",
        metavar="P",
        default=2000,
        type=int,
        help="TCP port to listen to (default: 2000)",
    )
    argparser.add_argument(
        "--res",
        metavar="WIDTHxHEIGHT",
        default="800x540",
        help="Window resolution (default: 800x540)",
    )
    argparser.add_argument(
        "--sync", action="store_true", help="Synchronous mode execution"
    )
    argparser.add_argument(
        "--filter",
        metavar="PATTERN",
        default="vehicle.*",
        help='Actor filter (default: "vehicle.*")',
    )
    argparser.add_argument(
        "-l",
        "--loop",
        action="store_true",
        dest="loop",
        help="Sets a new random destination upon reaching the previous one (default: False)",
    )
    argparser.add_argument(
        "-a",
        "--agent",
        type=str,
        choices=["Behavior", "Basic"],
        help="select which agent to run",
        default="Behavior",
    )
    argparser.add_argument(
        "-b",
        "--behavior",
        type=str,
        choices=["cautious", "normal", "aggressive"],
        help="Choose one of the possible agent behaviors (default: normal) ",
        default="normal",
    )
    argparser.add_argument(
        "-s",
        "--seed",
        help="Set seed for repeating executions (default: None)",
        default=None,
        type=int,
    )
    argparser.add_argument(
        "-ff",
        "--fileflag",
        help="Set flag for logging each frame into client_log",
        default=0,
        type=int,
    )

    args = argparser.parse_args()

    args.width, args.height = [int(x) for x in args.res.split("x")]
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(format="%(levelname)s: %(message)s", level=log_level)

    logging.info("listening to server %s:%s", args.host, args.port)
    ret_episode = 0
    ret_step = 0
    episode_counter = 0
    try:
        world, agent, clock, controller, display = init_sim(args)
        prev_timestamp = init_episode(world=world, clock=clock, agent=agent)
        while ret_episode != -1:
            frame_counter = 0
            while ret_step != -1:
                ret_episode, ret_step, prev_timestamp = await game_step(
                    episode_counter=episode_counter,
                    frame_counter=frame_counter,
                    controller=None,
                    display=display,
                    world=world,
                    agent=agent,
                    clock=clock,
                    prev_timestamp=prev_timestamp,
                )
                frame_counter += 1
            ret_step = 0
            world.restore_frame_state(0)
            print("Restored frame state")
            print("Finished episode ", episode_counter, " initializing next episode")
            episode_counter += 1
            clock.tick()
            world.world.tick()
            prev_timestamp = init_episode(world=world, clock=clock, agent=agent)

    except KeyboardInterrupt as e:
        print("\n")
        if world is not None:
            settings = world.world.get_settings()
            settings.synchronous_mode = False
            settings.fixed_delta_seconds = None
            world.world.apply_settings(settings)
            world.destroy()
            world = None
        pygame.quit()
        return -1

    except Exception as e:
        print(traceback.format_exc())

    finally:
        if world is not None:
            settings = world.world.get_settings()
            settings.synchronous_mode = False
            settings.fixed_delta_seconds = None
            world.world.apply_settings(settings)
            world.destroy()

        print("Bye, bye")
        pygame.quit()
        return -1


if __name__ == "__main__":
    asyncio.run(main())
