
import os, sys, pickle, math
from copy import deepcopy
from turtle import distance

from scipy import io
import numpy as np
import matplotlib.pyplot as plt

from load_data import load_lidar_data, load_joint_data, joint_name_to_index
from utils import *
import random
import logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOGLEVEL", "INFO"))

class map_t:
    """
    This will maintain the occupancy grid and log_odds. You do not need to change anything
    in the initialization
    """
    def __init__(s, resolution=0.05):
        s.resolution = resolution
        s.xmin, s.xmax = -20, 20
        s.ymin, s.ymax = -20, 20
        s.szx = int(np.ceil((s.xmax-s.xmin)/s.resolution+1))
        s.szy = int(np.ceil((s.ymax-s.ymin)/s.resolution+1))

        # binarized map and log-odds
        s.cells = np.zeros((s.szx, s.szy), dtype=np.float64)
        s.log_odds = np.zeros(s.cells.shape, dtype=np.float64)

        # value above which we are not going to increase the log-odds
        # similarly we will not decrease log-odds of a cell below -max
        s.log_odds_max = 5e6
        # number of observations received yet for each cell
        s.num_obs_per_cell = np.zeros(s.cells.shape, dtype=np.uint64)

        # we call a cell occupied if the probability of
        # occupancy P(m_i | ... ) is >= occupied_prob_thresh
        s.occupied_prob_thresh = 0.6
        s.log_odds_thresh = np.log(s.occupied_prob_thresh/(1-s.occupied_prob_thresh))

    def grid_cell_from_xy(s, x, y):
        """
        x and y are 1-dimensional arrays, compute the cell indices in the map corresponding
        to these (x,y) locations. You should return an array of shape 2 x len(x). Be
        careful to handle instances when x/y go outside the map bounds, you can use
        np.clip to handle these situations.
        """
        x=np.clip(x,s.xmin, s.xmax)
        y=np.clip(y,s.ymin, s.ymax)
        indices=np.zeros((2,len(x)))
        indices[0,:]=np.round((x-s.xmin)/s.resolution)
        indices[1,:]=np.round((y-s.ymin)/s.resolution)
        return indices.astype('int32')

    def create_traj_map(s,x,y):
        x=np.clip(x,s.xmin, s.xmax)
        y=np.clip(y,s.ymin, s.ymax)
        indices=np.zeros((2,len(x)))
        indices[0,:]=np.round((x-s.xmin)/s.resolution)
        indices[1,:]=np.round((y-s.ymin)/s.resolution)
        indices=indices.astype('int32')
        traj_cells = np.zeros((s.szx, s.szy), dtype=np.int8)
        for i in range(len(x)): traj_cells[indices[0,i],indices[1,i]]=1
        return traj_cells

class slam_t:
    """
    s is the same as self. In Python it does not really matter
    what we call self, s is shorter. As a general comment, (I believe)
    you will have fewer bugs while writing scientific code if you
    use the same/similar variable names as those in the mathematical equations.
    """
    def __init__(s, resolution=0.05, Q=1e-3*np.eye(3),
                 resampling_threshold=0.3):
        s.init_sensor_model()

        # dynamics noise for the state (x,y,yaw)
        # s.Q = 1e-8*np.eye(3)
        s.Q = Q

        # we resample particles if the effective number of particles
        # falls below s.resampling_threshold*num_particles
        s.resampling_threshold = resampling_threshold

        # initialize the map
        s.map = map_t(resolution)
        

    def read_data(s, src_dir, idx=0, split='train'):
        """
        src_dir: location of the "data" directory
        """
        logging.info('> Reading data')
        s.idx = idx
        s.lidar = load_lidar_data(os.path.join(src_dir,
                                               'data/%s/%s_lidar%d'%(split,split,idx)))
        s.joint = load_joint_data(os.path.join(src_dir,
                                               'data/%s/%s_joint%d'%(split,split,idx)))

        # finds the closets idx in the joint timestamp array such that the timestamp
        # at that idx is t
        s.find_joint_t_idx_from_lidar = lambda t: np.argmin(np.abs(s.joint['t']-t))
        s.if_init=False
    def init_sensor_model(s):
        # lidar height from the ground in meters
        s.head_height = 0.93 + 0.33
        s.lidar_height = 0.15

        # dmin is the minimum reading of the LiDAR, dmax is the maximum reading
        s.lidar_dmin = 1e-3
        s.lidar_dmax = 30
        s.lidar_angular_resolution = 0.25
        # these are the angles of the rays of the Hokuyo
        s.lidar_angles = np.arange(-135,135+s.lidar_angular_resolution,
                                   s.lidar_angular_resolution)*np.pi/180.0

        # sensor model lidar_log_odds_occ is the value by which we would increase the log_odds
        # for occupied cells. lidar_log_odds_free is the value by which we should decrease the
        # log_odds for free cells (which are all cells that are not occupied)
        s.lidar_log_odds_occ = np.log(9)
        s.lidar_log_odds_free = np.log(1/9.)

    def init_particles(s, n=100, p=None, w=None, t0=0):
        """
        n: number of particles
        p: xy yaw locations of particles (3xn array)
        w: weights (array of length n)
        """
        s.n = n
        s.p = deepcopy(p) if p is not None else np.zeros((3,s.n), dtype=np.float64)
        s.w = deepcopy(w) if w is not None else np.ones(n)/float(s.n)

    @staticmethod
    def stratified_resampling(p, w):
        """
        resampling step of the particle filter, takes p = 3 x n array of
        particles with w = 1 x n array of weights and returns new particle
        locations (number of particles n remains the same) and their weights
        """
        c=w[0]
        i=0
        n=len(w)
        new_p=p
        for m in range(n):
            u=(random.uniform(0, 1)+m)/n
            while c<u:
                i+=1
                c+=w[i]
            new_p[:,m]=p[:,i]
        new_w=np.array([1/n]*n)
        return new_p,new_w

    @staticmethod
    def log_sum_exp(w):
        return w.max() + np.log(np.exp(w-w.max()).sum())

    def rays2world(s, p, d, head_angle=0, neck_angle=0, angles=None):
        """
        p is the pose of the particle (x,y,yaw)
        angles = angle of each ray in the body frame (this will usually
        be simply s.lidar_angles for the different lidar rays)
        d = is an array that stores the distance of along the ray of the lidar, for each ray (the length of d has to be equal to that of angles, this is s.lidar[t]['scan'])
        Return an array 2 x num_rays which are the (x,y) locations of the end point of each ray
        in world coordinates
        """
        # # make sure each distance >= dmin and <= dmax, otherwise something is wrong in reading
        # # the data
        length=len(s.lidar_angles)
        d=np.clip(d, s.lidar_dmin, s.lidar_dmax)
        # 1. from lidar distances to points in the LiDAR frame
        vl=np.zeros((4,length))
        vl[0,:]=d*np.cos(s.lidar_angles)
        vl[1,:]=d*np.sin(s.lidar_angles)
        vl[3,:]=np.ones(length)
        # 2. from LiDAR frame to the body frame
        Rbl=euler_to_so3(0, head_angle, neck_angle) #get R from body to lidar frame
        T_bl=np.block([[Rbl,np.array([[0],[0],[0.15]])] , [np.array([0,0,0,1])] ])    #get T_inv from lidar to body frame
        v_b=T_bl@vl
        # 3. from body frame to world frame
        T_wb=np.block([[euler_to_so3(0, 0, p[2]),np.array([[p[0]],[p[1]],[1.263]])] , [np.array([0,0,0,1])] ])   #get T_inv from lidar to body frame
        v_w=T_wb@v_b
        return v_w[0],v_w[1]


    def get_control(s, t):
        """
        Use the pose at time t and t-1 to calculate what control the robot could have taken
        at time t-1 at state (x,y,th)_{t-1} to come to the current state (x,y,th)_t. We will
        assume that this is the same control that the robot will take in the function dynamics_step
        below at time t, to go to time t-1. need to use the smart_minus_2d function to get the difference of the two poses and we will simply set this to be the control (delta x, delta y, delta theta)
        """
        if t == 0:
            return np.zeros(3)
        else:
            return smart_minus_2d(s.lidar[t]['xyth'],s.lidar[t-1]['xyth'])

        

    def dynamics_step(s, t):
        """"
        Compute the control using get_control and perform that control on each particle to get the updated locations of the particles in the particle filter, remember to add noise using the smart_plus_2d function to each particle
        """
        #### TODO: XXXXXXXXXXX
        u=s.get_control(t)
        for i in range(s.n):
            s.p[:,i] =smart_plus_2d(s.p[:,i],u)
            s.p[:,i] =smart_plus_2d(s.p[:,i], np.random.multivariate_normal(np.zeros(3), s.Q))

    @staticmethod
    def update_weights(w, obs_logp):
        """
        Given the observation log-probability and the weights of particles w, calculate the
        new weights as discussed in the writeup. Make sure that the new weights are normalized
        """
        ############ !!!!!!!!!!! MAY RAISE OVERFLOW !!!!!!!!!!! ################################
        # w_new=np.multiply(np.exp(obs_logp),w) 
        # w_new=w_new/np.linalg.norm(w_new,1)
        # return w_new

        ######################################## Change to log 'coords' ################################################
        log_w_new = np.log(w) + obs_logp
        log_w_new = log_w_new - slam_t.log_sum_exp(log_w_new)
        w_new=np.exp(log_w_new)
        return w_new

    def observation_step(s, t):
        """
        This function does the following things
            1. updates the particles using the LiDAR observations
            2. updates map.log_odds and map.cells using occupied cells as shown by the LiDAR data

        Some notes about how to implement this.
            1. As mentioned in the writeup, for each particle
                (a) First find the head, neck angle at t (this is the same for every particle)
                (b) Project lidar scan into the world frame (different for different particles)
                (c) Calculate which cells are obstacles according to this particle for this scan,
                calculate the observation log-probability
            2. Update the particle weights using observation log-probability
            3. Find the particle with the largest weight, and use its occupied cells to update the map.log_odds and map.cells.
        You should ensure that map.cells is recalculated at each iteration (it is simply the binarized version of log_odds). map.log_odds is of course maintained across iterations.
        """
        tj = s.find_joint_t_idx_from_lidar(s.lidar[t]['t'])
        neck_angle= s.joint['head_angles'][0,tj]
        head_angle= s.joint['head_angles'][1,tj]
        logPs=np.zeros(s.n)
        for i in range(s.n):
            xs,ys =s.rays2world(s.p[:,i],s.lidar[t]['scan'],head_angle,neck_angle) # Get world frame positions of obstacles
            indices=s.map.grid_cell_from_xy(xs,ys)                                # Get map indices of obstacles
            temp=s.map.cells[indices[0,:],indices[1,:]]
            logPs[i] =(temp>0.5).sum() # Compute log prob
            
        ####################################### Update map  ######################################
        ### Update weight and resample
        # print("before",s.w,logPs)
        s.w=s.update_weights(s.w,logPs)
        max_index=np.argmax(s.w)
        xs,ys=s.rays2world(s.p[:,max_index],s.lidar[t]['scan'],head_angle,neck_angle) # Get world frame positions of obstacles
        indices=s.map.grid_cell_from_xy(xs,ys)
        particle_indices=s.map.grid_cell_from_xy([s.p[0,max_index]],[s.p[1,max_index]])

        # Update log_odds inside the vision
        ############### Find these piece of code online to be useful ####################################
        x_free = np.ndarray.flatten(np.linspace(particle_indices[0], indices[0,:], endpoint=False).astype('int32'))
        y_free = np.ndarray.flatten(np.linspace(particle_indices[1], indices[1,:], endpoint=False).astype('int32'))
        s.map.log_odds[indices[0,:], indices[1,:]]+= s.lidar_log_odds_occ*0.2
        s.map.log_odds[x_free, y_free] += s.lidar_log_odds_free*0.2
        s.map.log_odds = np.clip(s.map.log_odds, -s.map.log_odds_max, s.map.log_odds_max)
        ############### Find these piece of code online to be useful ####################################

        # creating binary map using log odds map
        if s.if_init == False: ####Initialize map
            s.map.cells[:, :] = 0.5
            s.map.cells[x_free, y_free] = 0
            s.map.cells[indices[0, :], indices[1, :]] = 1
            s.if_init = True
        else:
            s.map.cells[:, :] = 0.5
            s.map.cells[np.where(s.map.log_odds > s.map.log_odds_thresh)] = 1
            s.map.cells[np.where(s.map.log_odds < -s.map.log_odds_thresh)] = 0

        s.resample_particles()
        return s.p[:,max_index]
        

    def resample_particles(s):
        """
        Resampling is a (necessary) but problematic step which introduces a lot of variance
        in the particles. We should resample only if the effective number of particles
        falls below a certain threshold (resampling_threshold). A good heuristic to
        calculate the effective particles is 1/(sum_i w_i^2) where w_i are the weights
        of the particles, if this number of close to n, then all particles have about
        equal weights and we do not need to resample
        """
        e = 1/np.sum(s.w**2)
        logging.debug('> Effective number of particles: {}'.format(e))
        # print(s.w)
        if e/s.n < s.resampling_threshold:
            s.p, s.w = s.stratified_resampling(s.p, s.w)
            logging.debug('> Resampling')
